"""Tests for tests/known_failure_issues.py, the one program validation runs with the run's token."""

from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent / "validation"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "ejryan89-skill-core" / "scripts"))

import known_failures

from tests import known_failure_issues

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
ENTRY_POINT = REPOSITORY_ROOT / "tests" / "known_failure_issues.py"
# Every repository module the program loads, written out rather than derived, so a new import into the query, and so
# into the process the token reaches, fails here until it is named. None is a policy module but the query's own.
LOADED = sorted(
    [
        "deployer/__init__.py",
        "deployer/errors.py",
        "deployer/platform_support.py",
        *(
            f"skills/ejryan89-skill-core/scripts/{module}.py"
            for module in (
                "bounded_process",
                "closing_references",
                "console",
                "device_names",
                "executable_locator",
                "file_presence",
                "flat_text",
                "gh_shorthands",
                "git_client",
                "github_client",
                "github_remote",
                "job_tree",
                "json_reader",
                "link_probe",
                "markdown_sections",
                "module_assignments",
                "nesting_depth",
                "path_containment",
                "path_keys",
                "process_identity",
                "python_reader",
                "release_levels",
                "release_tags",
                "skill_menu",
                "suite_names",
                "typed_fields",
                "typed_text",
                "versions",
                "whole_number",
            )
        ),
        "tests/validation/known_failures.py",
        "tests/validation/validation_support.py",
        "tools/release_notes.py",
    ]
)
# Print each repository module the program's imports load, without running its main.
PROBE = (
    "import runpy, sys\n"
    "from pathlib import Path\n"
    "root = Path(sys.argv[2])\n"
    "runpy.run_path(sys.argv[1], run_name='probe')\n"
    "files = (Path(module.__file__).resolve() for module in list(sys.modules.values()) "
    "if getattr(module, '__file__', None))\n"
    "print('\\n'.join(sorted(file.relative_to(root).as_posix() for file in files if file.is_relative_to(root))))\n"
)


def test_the_program_loads_the_query_s_modules_and_no_policy_or_suite() -> None:
    result = subprocess.run(
        [sys.executable, "-B", "-c", PROBE, str(ENTRY_POINT), str(REPOSITORY_ROOT)],
        capture_output=True,
        encoding="utf-8",
        check=False,
        cwd=REPOSITORY_ROOT,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == LOADED


def test_it_prints_each_problem_and_fails_on_one() -> None:
    problems = ["#1 is CLOSED", "#2 cannot be asked"]
    out = io.StringIO()
    with mock.patch.object(known_failures, "open_issue_problems", return_value=problems):
        assert known_failure_issues.main([], out=out) == 1
    assert out.getvalue().splitlines() == [
        "FAILED #1 is CLOSED",
        "FAILED #2 cannot be asked",
        "Known-failure issues: 2 not shown open.",
    ]


def owed(*issues: int) -> list[known_failures.OwedIssue]:
    return [
        known_failures.OwedIssue(issue, "known_failures.json", f"lists case {index}")
        for index, issue in enumerate(issues)
    ]


@pytest.mark.parametrize(
    ("named", "last"),
    [
        ([], "Known-failure issues: none named, so GitHub was not asked."),
        # Two entries under one issue are one issue asked about.
        (owed(7, 7, 9), "Known-failure issues: all 2 named shown open."),
    ],
)
def test_its_last_line_tells_a_list_naming_nothing_from_one_whose_every_issue_is_open(
    named: list[known_failures.OwedIssue], last: str
) -> None:
    out = io.StringIO()
    with (
        mock.patch.object(known_failures, "open_issue_problems", return_value=[]),
        mock.patch.object(known_failures, "owed_issues", return_value=named),
    ):
        assert known_failure_issues.main([], out=out) == 0
    assert out.getvalue().splitlines() == [last]


@pytest.mark.parametrize("argument", ["--full", "-k", "--known-failure-issues", "extra"])
def test_any_argument_is_a_usage_error_before_github_is_asked(
    argument: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with (
        mock.patch.object(known_failures, "open_issue_problems") as asked,
        pytest.raises(SystemExit) as raised,
    ):
        known_failure_issues.main([argument])
    assert raised.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err
    asked.assert_not_called()
