"""Fixture tests for tests/validation/toolchain.py.

Each policy fails on a violating input and passes on a conforming one.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

import toolchain
from job_pool import Terminated
from toolchain import (
    PREREQUISITES,
    fence_install_problems,
    find_psscriptanalyzer,
    find_ruff,
    floor_copy_problems,
    hash_install_problems,
    lock_problems,
    locked_requirements,
    missing_prerequisites,
    outdated_prerequisites,
    powershell_json,
    read_tool_version,
    removed_install_problems,
    requirements_problems,
    tool_versions,
)

from deployer import platform_support

# The operating-system names this module states on purpose, with the reason, which the platform policy reads.
PLATFORM_ALLOWED = {
    ".exe": "names the PowerShell path a fake find_powershell returns, which run_script's fake runner compares with "
    "the command it is given; nothing here finds or starts a program by it",
}
REQUIREMENT_PACKAGES = ("ruff", "mypy")
# What validation calls the Bash it finds here: Git Bash on Windows, Bash elsewhere.
BASH = platform_support.BASH_NAME
CLEAN_REQUIREMENTS = "# Development dependencies.\n\nruff==0.16.10\n  # Indented comment.\nmypy==2.4.0\n"


class ToolchainFixtures(unittest.TestCase):
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
                "  - PowerShell 7 (pwsh): winget install --id Microsoft.PowerShell --installer-type wix",
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
        clean = CLEAN_REQUIREMENTS
        self.assertEqual([], requirements_problems(clean, REQUIREMENT_PACKAGES))
        self.assertEqual(
            ["mypy is pinned 0 times, not once"],
            requirements_problems(clean.replace("mypy==2.4.0\n", ""), REQUIREMENT_PACKAGES),
        )
        self.assertEqual(
            ["ruff is pinned 2 times, not once"], requirements_problems(clean + "ruff==0.17.0\n", REQUIREMENT_PACKAGES)
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
            ["  - ruff: python -m pip install --require-hashes --only-binary :all: -r requirements-dev.txt"],
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
                "docs/dependency-updates.md: winget install --id Microsoft.PowerShell --installer-type wix",
                "  - ruff 0.17.0 is older than the floor 0.17.3 in docs/dependency-updates.md: "
                "python -m pip install --require-hashes --only-binary :all: -r requirements-dev.txt",
            ],
            outdated_prerequisites(
                {BASH: (5, 3, 15), "ShellCheck": (0, 8, 0), "PowerShell 7 (pwsh)": None, "ruff": (0, 17, 0)},
                (3, 11, 3),
            ),
        )
        self.assertEqual(
            [],
            outdated_prerequisites(
                {BASH: (5, 2), "ShellCheck": (0, 9, 0), "PowerShell 7 (pwsh)": (7, 5, 3), "ruff": (0, 17, 3)},
                (3, 11, 4),
            ),
        )
        # A missing tool is reported by missing_prerequisites, not again here.
        self.assertEqual([], outdated_prerequisites({}, (3, 14, 7)))

    def test_a_tool_at_its_floor_needs_no_install_hint(self) -> None:
        # Only a tool reported is given its install command, so a platform with none for it reports nothing.
        with mock.patch.object(platform_support, "INSTALL_HINTS", {}):
            self.assertEqual([], outdated_prerequisites({BASH: (5, 3, 15), "ShellCheck": (0, 9, 0)}, (3, 11, 4)))

    def test_a_bash_older_than_the_floor_is_reported_with_its_install_command(self) -> None:
        # macOS's own /bin/bash is a 3.2, which reads text the Bash reader is held to differently.
        with mock.patch.object(platform_support, "INSTALL_HINTS", {BASH: "brew install bash"}):
            self.assertEqual(
                [f"  - {BASH} 3.2.57 is older than the floor 5.2 in docs/dependency-updates.md: brew install bash"],
                outdated_prerequisites({BASH: (3, 2, 57)}, (3, 11, 4)),
            )
            self.assertEqual(
                [
                    f"  - {BASH}: its version could not be read; the floor is 5.2 in docs/dependency-updates.md: "
                    "brew install bash"
                ],
                outdated_prerequisites({BASH: None}, (3, 11, 4)),
            )
            self.assertEqual([], outdated_prerequisites({BASH: (5, 2, 0)}, (3, 11, 4)))

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


@pytest.mark.parametrize(
    "added",
    [
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
    ],
)
def test_a_requirements_line_that_is_not_a_listed_pin_fails(added: str) -> None:
    problems = requirements_problems(CLEAN_REQUIREMENTS + added, REQUIREMENT_PACKAGES)
    assert any("line 6 is not name==version" in problem for problem in problems), problems


# Two hashes in the form pip-tools writes them, and a lock of two packages in its layout: each pin continued onto its
# hashes, and its `# via` comments after them.
FIRST_HASH = "--hash=sha256:" + "a" * 64
SECOND_HASH = "--hash=sha256:" + "b" * 64
LOCK = (
    "#\n# This file is autogenerated by pip-compile\n#\n"
    f"mypy==2.4.0 \\\n    {FIRST_HASH} \\\n    {SECOND_HASH}\n    # via -r requirements-dev.in\n"
    f"typing-extensions==4.16.0 \\\n    {FIRST_HASH}\n    # via\n    #   mypy\n"
)


def test_a_lock_names_each_package_with_its_version_and_hashes() -> None:
    locked, problems = locked_requirements(LOCK)
    assert problems == []
    assert {
        "mypy": toolchain.Locked("2.4.0", frozenset({FIRST_HASH, SECOND_HASH})),
        "typing-extensions": toolchain.Locked("4.16.0", frozenset({FIRST_HASH})),
    } == locked
    assert lock_problems(LOCK, "# The input.\nMypy==2.4.0\n-r other.in\n") == []


@pytest.mark.parametrize(
    "requirement",
    [
        pytest.param("ruff==0.17.1\n", id="no hash"),
        pytest.param("ruff>=0.17.1 \\\n    " + FIRST_HASH + "\n", id="unpinned"),
        pytest.param("ruff==0.17.1 \\\n    --hash=md5:" + "a" * 32 + "\n", id="another digest"),
        pytest.param("ruff==0.17.1 \\\n    --hash=sha256:abc\n", id="a short hash"),
        pytest.param('ruff==0.17.1 ; python_version > "3.11" \\\n    ' + FIRST_HASH + "\n", id="a marker"),
        pytest.param("--index-url https://example.invalid/simple\n", id="another index"),
        pytest.param("-r other.txt\n", id="an included file"),
        pytest.param("ruff @ https://example.invalid/ruff.whl\n", id="a URL"),
        pytest.param("mypy==2.4.0 \\\n    " + FIRST_HASH + "\n", id="a package locked twice"),
    ],
)
def test_a_lock_requirement_that_is_not_a_hashed_pin_fails(requirement: str) -> None:
    _, problems = locked_requirements(LOCK + requirement)
    assert len(problems) == 1, problems


def test_a_lock_that_departs_from_its_input_or_the_development_lock_fails() -> None:
    assert lock_problems(LOCK, "mypy==2.5.0\n") == ["the input pins mypy==2.5.0, which the lock pins at 2.4.0"]
    assert lock_problems(LOCK, "ruff==0.17.1\n") == ["the input pins ruff==0.17.1, which the lock does not hold"]
    shared = {"mypy": toolchain.Locked("2.4.0", frozenset({FIRST_HASH}))}
    assert lock_problems(LOCK, "", shared) == [
        "mypy is not locked as the development lock locks it, at 2.4.0 and its hashes"
    ]
    shared = {"pytest": toolchain.Locked("9.1.1", frozenset({FIRST_HASH}))}
    assert len(lock_problems(LOCK, "", shared)) == 1


def test_a_workflow_installs_only_its_locks_with_hash_checking() -> None:
    clean = "          python -m pip install --require-hashes --only-binary :all: -r requirements-dev.txt\n"
    assert hash_install_problems(clean, ("requirements-dev.txt",)) == []
    for drifted in (
        "          python -m pip install -r requirements-dev.txt\n",
        "          python -m pip install -r requirements-dev.txt --require-hashes --only-binary :all:\n",
        "          python -m pip install --require-hashes -r requirements-dev.txt\n",
        "          python -m pip install --only-binary :all: --require-hashes -r requirements-dev.txt\n",
        "          python -m pip install --require-hashes --only-binary :none: -r requirements-dev.txt\n",
        "          python -m pip install --require-hashes --only-binary :all: -r requirements-dev.in\n",
        "          python -m pip install --require-hashes --only-binary :all: -r requirements-dev.txt ruff\n",
        "          python -m pip install --require-hashes --only-binary :all: -r requirements-dev.txt -r\n",
        "          python -m pip install --require-hashes --only-binary :all:\n",
        "          python -m pip install ruff==0.17.1\n",
        # pip by another name, with an option before install, or with the words spaced apart.
        "          pip3 install ruff==0.17.1\n",
        "          pip3.11 install --require-hashes --only-binary :all: -r requirements-dev.in\n",
        "          python -m pip --quiet install --require-hashes --only-binary :all: -r requirements-dev.txt\n",
        "          python -m pip  install  ruff==0.17.1\n",
        "          C:/Python311/Scripts/pip.exe install ruff==0.17.1\n",
        "          uv pip install ruff==0.17.1\n",
        # pip quoted, a lock named with more after it, and -r after a flag clustered before it.
        "          'pip' install ruff==0.17.1\n",
        "          python -m pip install --require-hashes --only-binary :all: -r requirements-dev.txtX\n",
        "          python -m pip install --require-hashes --only-binary :all: -qr requirements-dev.txt\n",
    ):
        assert [
            "line 2 does not install only ['requirements-dev.txt'] with --require-hashes --only-binary :all:: "
            + repr(drifted.strip())
        ] == hash_install_problems(clean + drifted, ("requirements-dev.txt",))
    # A workflow that installs no lock may run no pip install, and pip that installs nothing is not one.
    assert len(hash_install_problems(clean, ())) == 1
    assert hash_install_problems("      - run: python -m pip --version\n        cache: pip\n", ()) == []
    # A program whose name only ends in pip is not pip.
    assert hash_install_problems("          Xpip install ruff==0.17.1\n", ()) == []


@pytest.mark.parametrize(
    "install",
    [
        pytest.param(
            "python -m pip install --require-hashes --only-binary :all: -rrequirements-dev.txt", id="-r<file>"
        ),
        pytest.param(
            "python -m pip install --require-hashes --only-binary :all: --requirement=requirements-dev.txt",
            id="--requirement=<file>",
        ),
        pytest.param(
            "python -m pip install --require-hashes --only-binary :all: --requirement requirements-dev.txt",
            id="--requirement <file>",
        ),
        pytest.param(
            "python -m pip install --require-hashes --only-binary :all: --requirem requirements-dev.txt",
            id="a prefix that names --requirement alone",
        ),
        pytest.param(
            "python -m pip install --require-hashes --only-binary :all: -r 'requirements-dev.txt'", id="a quoted lock"
        ),
        pytest.param(
            "python -m pip install \\\n  --require-hashes --only-binary :all: \\\n  -r requirements-dev.txt",
            id="continued by backslashes",
        ),
        pytest.param(
            "python -m pip install `\n  --require-hashes --only-binary :all: -r requirements-dev.txt",
            id="continued by a backtick",
        ),
    ],
)
def test_a_lock_installed_with_hash_checking_passes_in_each_form_pip_reads(install: str) -> None:
    locks = ("requirements-dev.txt",)
    assert hash_install_problems(f"        run: |\n{install}\n", locks) == []
    assert fence_install_problems(f"```bash\n{install}\n```\n", locks) == []


@pytest.mark.parametrize("join", ["&&", "||", ";", "|", "&"])
def test_each_pip_install_among_the_commands_of_one_line_is_read(join: str) -> None:
    locked = "python -m pip install --require-hashes --only-binary :all: -r requirements-dev.txt"
    for unpinned in ("python -m pip install ruff", "pip3 install -r requirements-dev.txt"):
        assert hash_install_problems(f"        run: {locked} {join} {unpinned}\n", ("requirements-dev.txt",)) == [
            "line 1 does not install only ['requirements-dev.txt'] with --require-hashes --only-binary :all:: "
            + repr(unpinned)
        ]
    # The documented install of a lock is read wherever it stands among the commands, so the second one here is.
    document = f"```bash\npython -m pip install pip-tools==7.6.2 {join} pip install -r requirements-dev.txt\n```\n"
    assert fence_install_problems(document, ("requirements-dev.txt",)) == [
        "line 2 does not install only ['requirements-dev.txt'] with --require-hashes --only-binary :all:: "
        "'pip install -r requirements-dev.txt'"
    ]
    assert hash_install_problems(f"        run: {locked} {join} {locked}\n", ("requirements-dev.txt",)) == []


@pytest.mark.parametrize(
    "drifted",
    [
        pytest.param("python -m pip install -rrequirements-dev.txt", id="-r<file>"),
        pytest.param("python -m pip install --requirement=requirements-dev.txt", id="--requirement=<file>"),
        pytest.param("python -m pip install --requirem requirements-dev.txt", id="a prefix naming --requirement"),
        pytest.param("python -m pip install --req=requirements-dev.txt", id="an ambiguous prefix"),
        pytest.param("python -m pip install -qr requirements-dev.txt", id="-r clustered after a flag"),
        pytest.param("python -m pip install -Uqrrequirements-dev.txt", id="-r<file> clustered after flags"),
        pytest.param("python -m pip install --req=requirements=dev.txt", id="an ambiguous prefix with = in its value"),
        pytest.param("python -m pip install '--req' requirements-dev.txt", id="a quoted ambiguous prefix"),
        pytest.param("python -m pip install --r requirements-dev.txt", id="the shortest prefix"),
        pytest.param("python -m pip install \\\n  -r requirements-dev.txt", id="continued by a backslash"),
        pytest.param("python -m pip install `\n  -r requirements-dev.txt", id="continued by a backtick"),
        pytest.param(
            "python -m pip install --require-hashes --only-binary :all: \\\n  -r requirements-dev.txt ruff",
            id="continued onto a package beside the lock",
        ),
    ],
)
def test_a_documented_install_from_a_requirements_file_is_read_in_each_form_pip_reads(drifted: str) -> None:
    locks = ("requirements-dev.txt",)
    command = re.sub(r"[\\`]\n", " ", drifted)
    assert fence_install_problems(f"Install them:\n\n```bash\n{drifted}\n```\n", locks) == [
        "line 4 does not install only ['requirements-dev.txt'] with --require-hashes --only-binary :all:: "
        + repr(command)
    ]


@pytest.mark.parametrize(
    "install",
    [
        pytest.param("python -m pip install -crequirements-dev.txt ruff==0.17.1", id="-c takes the r as its value"),
        pytest.param("python -m pip install -i https://example.invalid/simple ruff==0.17.1", id="an index"),
        pytest.param("python -m pip install --root /opt ruff==0.17.1", id="a long option that is no prefix"),
        pytest.param("python -m pip install --reqX requirements-dev.txt", id="a long option that only starts like one"),
        pytest.param("python -m pip install -- ruff==0.17.1", id="the end of the options"),
        pytest.param("python -m pip install pip-tools==7.6.2 \\\\\n-r requirements-dev.txt", id="an escaped backslash"),
    ],
)
def test_a_documented_install_naming_no_requirements_file_is_not_one(install: str) -> None:
    assert fence_install_problems(f"```bash\n{install}\n```\n", ("requirements-dev.txt",)) == []


def test_a_documented_install_from_a_requirements_file_checks_hashes_as_ci_does() -> None:
    locks = ("requirements-dev.txt",)
    clean = "```powershell\npython -m pip install --require-hashes --only-binary :all: -r requirements-dev.txt\n```\n"
    assert fence_install_problems(clean, locks) == []
    # An install that names no requirements file, such as pip-tools in a virtual environment of its own, and an
    # install in prose, outside a fence, are not documented installs of a lock.
    assert fence_install_problems("```bash\npython -m pip install pip-tools==7.6.2\n```\n", locks) == []
    assert fence_install_problems("Run `python -m pip install -r requirements-dev.txt` once.\n", locks) == []
    for drifted in (
        "python -m pip install -r requirements-dev.txt",
        "~/.venvs/coding-agent-skills/bin/python -m pip install -r requirements-dev.txt",
        "python -m pip install --require-hashes -r requirements-dev.txt",
        "python -m pip install --require-hashes --only-binary :all: -r requirements-dev.in",
        "python -m pip install --requirement requirements-dev.txt",
        "> python -m pip install -r requirements-dev.txt",
    ):
        document = f"Install them:\n\n```bash\n{drifted}\n```\n"
        assert fence_install_problems(document, locks) == [
            "line 4 does not install only ['requirements-dev.txt'] with --require-hashes --only-binary :all:: "
            + repr(drifted)
        ], drifted


def test_a_copy_of_a_floor_that_is_missing_or_names_another_version_fails() -> None:
    pattern = r"Codex CLI (\S+) or newer"
    assert floor_copy_problems("| Codex CLI 0.88.0 or newer (optional) |", pattern, "0.88.0") == []
    assert floor_copy_problems("| Codex CLI 0.87.0 or newer (optional) |", pattern, "0.88.0") == [
        "0.87.0 is named where the floor is 0.88.0"
    ]
    assert floor_copy_problems("Codex CLI 0.88.0 or newer, then Codex CLI 0.90.1 or newer", pattern, "0.88.0") == [
        "0.90.1 is named where the floor is 0.88.0"
    ]
    assert floor_copy_problems("| Codex CLI (optional) |", pattern, "0.88.0") == [
        f"no copy of the floor matches {pattern!r}"
    ]


def test_an_input_may_hold_its_includes_as_written_and_nothing_else_like_them() -> None:
    includes = ("-r requirements-dev.in", "-c requirements-dev.txt")
    text = "-r requirements-dev.in\n-c requirements-dev.txt\nruff==0.16.10\nmypy==2.4.0\n"
    assert requirements_problems(text, REQUIREMENT_PACKAGES, includes) == []
    assert len(requirements_problems(text + "-r other.in\n", REQUIREMENT_PACKAGES, includes)) == 1


def run_script(result: platform_support.ToolResult) -> tuple[list[object], list[dict[str, str]]]:
    """The runner's list for a fake pwsh's result, and each environment it was started with."""
    seen: list[dict[str, str]] = []

    def fake(command: list[str], environment: dict[str, str]) -> platform_support.ToolResult:
        seen.append(environment)
        assert command[:5] == ["C:/tools/pwsh.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command"]
        assert json.loads(Path(environment["INPUTS"]).read_text(encoding="utf-8")) == ["a", 1]
        return result

    with (
        mock.patch.object(toolchain, "find_powershell", return_value="C:/tools/pwsh.exe"),
        mock.patch.object(platform_support, "run_tool", side_effect=fake),
    ):
        return powershell_json("Get-Thing", ["a", 1], "INPUTS", "Could not run", {"FLOOR": "1.0"}), seen


class RemovedInstallFixtures(unittest.TestCase):
    def test_a_homebrew_install_of_a_removed_package_is_reported_wherever_it_sits(self) -> None:
        for text in (
            "brew install --cask powershell codex copilot-cli",
            "| PowerShell 7 | `brew install --cask powershell` | the `.pkg` |",
            "brew install powershell",
            "brew install --cask 'powershell'",
            "brew reinstall homebrew/cask/powershell",
            "  run: |\n    brew update\n    brew install --cask powershell --no-quarantine\n",
        ):
            with self.subTest(text=text):
                problems = removed_install_problems(text)
                self.assertEqual(1, len(problems), problems)
                self.assertIn("installs Homebrew's powershell, which Homebrew no longer has", problems[0])
        self.assertIn("line 3:", removed_install_problems("a\nb\nbrew install --cask powershell\n")[0])

    def test_an_install_that_still_works_is_not_reported(self) -> None:
        for text in (
            "brew install bash shellcheck gh",
            "brew install --cask codex copilot-cli",
            "sudo snap install powershell --classic",
            "winget install --id Microsoft.PowerShell --installer-type wix",
            "brew install --cask codex | grep powershell",
            "brew install gh; echo powershell",
            "the `.pkg` from PowerShell's releases; Homebrew's `powershell@preview` installs `pwsh-preview`",
        ):
            with self.subTest(text=text):
                self.assertEqual([], removed_install_problems(text))


class PowerShellJsonRunner(unittest.TestCase):
    def test_the_last_line_s_list_is_the_result_and_the_inputs_reach_the_script(self) -> None:
        found, seen = run_script(platform_support.ToolResult(0, 'warming up\n[{"line": 3}]\n\n'))
        self.assertEqual([{"line": 3}], found)
        self.assertEqual("1.0", seen[0]["FLOOR"])

    @mock.patch.object(platform_support, "WINDOWS", True)
    def test_a_pwsh_ended_before_output_is_a_termination(self) -> None:
        with self.assertRaises(Terminated):
            run_script(platform_support.ToolResult(2_147_483_652, ""))


@pytest.mark.parametrize(
    "result",
    [
        pytest.param(platform_support.ToolResult(1, "[]\n"), id="failed exit"),
        pytest.param(platform_support.ToolResult(0, '{"line": 3}\n'), id="an object"),
        pytest.param(platform_support.ToolResult(0, "not json\n"), id="not json"),
        # JSON json_reader refuses is output that is not a list too, never an error of its own.
        pytest.param(platform_support.ToolResult(0, '[{"line": 3, "line": 4}]\n'), id="a repeated key"),
        pytest.param(platform_support.ToolResult(0, "[NaN]\n"), id="a constant JSON does not define"),
        pytest.param(platform_support.ToolResult(0, ""), id="no output"),
    ],
)
def test_a_failed_exit_or_output_that_is_not_a_list_is_a_failure_naming_the_status(
    result: platform_support.ToolResult,
) -> None:
    with pytest.raises(AssertionError) as raised:
        run_script(result)
    assert str(raised.value).startswith(f"Could not run (exit code {result.returncode}):\n")


class PytestPrerequisiteFixtures(unittest.TestCase):
    def test_missing_pytest_is_reported_with_the_install_command(self) -> None:
        self.assertIn(("pytest", "pytest", toolchain.find_pytest), toolchain.PREREQUISITES)
        with (
            mock.patch.object(platform_support, "find_on_path", return_value=None),
            mock.patch.object(platform_support, "find_executable", return_value=None),
        ):
            self.assertIsNone(toolchain.find_pytest())
        python = "python" if platform_support.WINDOWS else "python3"
        self.assertEqual(
            [f"  - pytest: {python} -m pip install --require-hashes --only-binary :all: -r requirements-dev.txt"],
            toolchain.missing_prerequisites((("pytest", "pytest", lambda: None),)),
        )

    def test_pytest_older_than_its_pin_stops_the_run(self) -> None:
        outdated = toolchain.outdated_prerequisites({"pytest": (9, 0, 3)}, (3, 11, 4))
        self.assertEqual(1, len(outdated))
        self.assertIn("pytest 9.0.3 is older than the floor 9.2.0 in docs/dependency-updates.md", outdated[0])
