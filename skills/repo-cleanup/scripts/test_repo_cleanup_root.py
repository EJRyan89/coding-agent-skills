"""The repositories root a sweep is given: a plugin's repos_root option reaches the script as typed, checked against no
allowlist, so an empty value must never become the working directory and a relative one never resolve against it."""

from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

import repo_cleanup as rc
from test_repo_cleanup import GIT_CONFIG, FakeGitHub

# The settings conftest.py adds to the global configuration of every home this suite's git runs in.
git_config = GIT_CONFIG

EMPTY = "--repos-root is empty; set the root directory for your git repositories"


@pytest.mark.parametrize(
    ("root", "reason"),
    [
        ("", EMPTY),
        ("  ", EMPTY),
        ("repos", "--repos-root repos is not an absolute path"),
        ("${user_config.repos_root}", "--repos-root ${user_config.repos_root} is not an absolute path"),
    ],
)
@pytest.mark.parametrize("target", [[], ["my repo"]])
def test_a_sweep_refuses_an_empty_or_relative_root_before_asking_github_anything(
    root: str, reason: str, target: list[str], tmp_path: Path
) -> None:
    github = FakeGitHub()
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = rc.main(
            ["sweep", "--repos-root", root, "--plans", str(tmp_path / "plans"), *target], rc.Services(gh=github)
        )
    assert (code, output.getvalue().splitlines()) == (1, [f"FAILED\t{reason}"])
    assert github.calls == []
    assert not (tmp_path / "plans").exists()
