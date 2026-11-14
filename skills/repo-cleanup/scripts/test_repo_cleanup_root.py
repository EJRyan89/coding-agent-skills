"""The repositories root a sweep is given: a plugin's repos_root option reaches the script as typed, checked against no
allowlist, so an empty value must never become the working directory and a relative one never resolve against it."""

from __future__ import annotations

import contextlib
import io
import re
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

import repo_cleanup as rc
from test_repo_cleanup import GIT_CONFIG, FakeGitHub, Fixture, build_template, stat_failing_at

# The settings conftest.py adds to the global configuration of every home this suite's git runs in.
git_config = GIT_CONFIG


def setUpModule() -> None:
    """Build the repositories every Fixture test starts from, with Git isolated as each test isolates it."""
    build_template()


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
    root: str, reason: str, target: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    github = FakeGitHub()
    output = io.StringIO()
    # A regression that reached the plans directory would make it here, never in the user's temporary directory.
    monkeypatch.setattr(rc, "new_plans_directory", lambda: Path(tempfile.mkdtemp(dir=tmp_path)))
    with contextlib.redirect_stdout(output):
        code = rc.main(["sweep", "--repos-root", root, *target], rc.Services(gh=github))
    assert (code, output.getvalue().splitlines()) == (1, [f"FAILED\t{reason}"])
    assert github.calls == []
    assert list(tmp_path.iterdir()) == []


class ExaminedRootTests(Fixture):
    """A target, its .git, or the root a stat cannot examine is refused with the error, never read as absent."""

    def discover(self, *target: str) -> list[str]:
        return rc.discover(target[0] if target else None, str(self.repos), rc.Services(gh=self.github))

    def test_a_target_root_or_git_folder_it_cannot_examine_is_refused_with_the_error_never_as_absent(self) -> None:
        # Path.is_dir answers False for a stat that fails, which read as "not a git repository" or "not a directory"
        # for a path the account could not examine.
        error = f"[Errno 13] Permission denied: {str(self.clone / '.git')!r}"
        for target in ("my repo", str(self.clone)):
            with (
                self.subTest(target=target),
                stat_failing_at(self.clone / ".git"),
                self.assertRaisesRegex(
                    rc.CleanupError, f"^{re.escape(f'{self.clone} cannot be examined for a .git: {error}')}$"
                ),
            ):
                self.discover(target)
        with (
            stat_failing_at(self.clone / ".git"),
            self.assertRaisesRegex(
                rc.CleanupError, f"^{re.escape(f'{self.clone} cannot be examined for a .git: {error}')}$"
            ),
        ):
            rc.require_repository(self.clone)
        refused = f"^{re.escape(f'--repos-root {self.repos} cannot be examined: Permission denied')}$"
        with stat_failing_at(self.repos), self.assertRaisesRegex(rc.CleanupError, refused):
            self.discover()
        self.assertEqual([], self.github.calls)

    def test_a_git_file_or_a_file_in_the_roots_place_is_still_no_repository_and_no_directory(self) -> None:
        (self.repos / "linked").mkdir()
        (self.repos / "linked" / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
        with self.assertRaisesRegex(rc.CleanupError, "^linked is not a git repository$"):
            self.discover("linked")
        root = self.root / "a file"
        root.write_text("", encoding="utf-8")
        with self.assertRaisesRegex(rc.CleanupError, f"^{re.escape(f'--repos-root {root} is not a folder')}$"):
            rc.discover(None, str(root), rc.Services(gh=self.github))
