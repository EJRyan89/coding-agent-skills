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
                "flat_text",
                "git_client",
                "github_client",
                "github_remote",
                "job_tree",
                "json_reader",
                "link_probe",
                "markdown_sections",
                "nesting_depth",
                "path_containment",
                "path_keys",
                "process_identity",
                "release_levels",
                "release_tags",
                "skill_menu",
                "suite_names",
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


@pytest.mark.parametrize(("problems", "status"), [([], 0), (["#1 is CLOSED", "#2 cannot be asked"], 1)])
def test_it_prints_each_problem_and_fails_on_one(problems: list[str], status: int) -> None:
    out = io.StringIO()
    with mock.patch.object(known_failures, "open_issue_problems", return_value=problems):
        assert known_failure_issues.main([], out=out) == status
    lines = out.getvalue().splitlines()
    assert lines[:-1] == [f"FAILED {problem}" for problem in problems]
    assert lines[-1] == f"Known-failure issues: {len(problems)} not shown open."


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
