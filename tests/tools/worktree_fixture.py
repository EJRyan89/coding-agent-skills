"""The hub and its task worktree the hub guard's and the worktrees suites run in, built once per process and copied.

tests/tools/test_hub_guard.py and tests/tools/test_worktrees.py import it from beside them; it holds no test.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import ClassVar

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKTREES_SCRIPT = REPOSITORY_ROOT / "tools" / "worktrees.py"
GUARD_SCRIPT = REPOSITORY_ROOT / "tools" / "hub_guard.py"

HUB = "Repo With Spaces"
TREE = Path(".claude") / "worktrees" / "feat-demo"


def isolated_environment() -> dict[str, str]:
    """A copy of this process's environment, whose git conftest.py isolates, for a test to hand a command or change."""
    return dict(os.environ)


def run_git(cwd: Path, environment: dict[str, str], *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *arguments], capture_output=True, text=True, env=environment, check=False
    )


def run_script(
    script: Path, arguments: list[str], cwd: Path, environment: dict[str, str], stdin: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", str(script), *arguments],
        cwd=cwd,
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        check=False,
    )


def build_hub(hub: Path, environment: dict[str, str]) -> None:
    """A hub on main with one commit, the guard enabled, and a feat/demo task worktree made by `new`."""
    hub.mkdir()
    run_git(hub, environment, "init", "-q", "-b", "main").check_returncode()
    (hub / "README.md").write_text("hub\n", encoding="utf-8")
    (hub / ".gitignore").write_text("local.json\n.claude/worktrees/\n", encoding="utf-8")
    for arguments in (["add", "."], ["commit", "-q", "-m", "initial"]):
        run_git(hub, environment, *arguments).check_returncode()
    created = run_script(WORKTREES_SCRIPT, ["new", "feat", "demo"], cwd=hub, environment=environment)
    if created.returncode != 0:
        raise AssertionError(created.stderr)
    run_git(hub, environment, "config", "coding-agent-skills.hubGuard", "true").check_returncode()


# Building the hub starts about eight git processes and a Python one, so each process builds it once and every test
# gets a copy. The hub and its worktree name each other by absolute path, so `git worktree repair` reconnects them.
TEMPLATE_DIRECTORY = tempfile.TemporaryDirectory(prefix="worktrees-template.")
TEMPLATE_LOCK = threading.Lock()
TEMPLATE_BUILT = False


def copy_hub(hub: Path, environment: dict[str, str]) -> None:
    global TEMPLATE_BUILT
    root = Path(TEMPLATE_DIRECTORY.name).resolve()
    with TEMPLATE_LOCK:
        if not TEMPLATE_BUILT:
            build_hub(root / HUB, isolated_environment())
            TEMPLATE_BUILT = True
    shutil.copytree(root / HUB, hub, symlinks=True)
    repaired = run_git(hub, environment, "worktree", "repair", str(hub / TREE))
    if repaired.returncode != 0:
        raise AssertionError(repaired.stderr)


class HubTestCase(unittest.TestCase):
    """A test in its own copy of the hub, which runs script, the tool under test, as a program."""

    script: ClassVar[Path]

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="worktrees-test.")
        self.root = Path(self._temporary.name).resolve()
        self.environment = isolated_environment()
        self.hub = self.root / HUB
        copy_hub(self.hub, self.environment)
        self.worktrees = self.hub / ".claude" / "worktrees"
        self.tree = self.hub / TREE

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def git(self, cwd: Path, *arguments: str) -> str:
        result = run_git(cwd, self.environment, *arguments)
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout

    def enable(self, value: str | None) -> None:
        if value is None:
            subprocess.run(
                ["git", "-C", str(self.hub), "config", "--unset", "coding-agent-skills.hubGuard"],
                env=self.environment,
                check=False,
            )
        else:
            self.git(self.hub, "config", "coding-agent-skills.hubGuard", value)

    def run_script(
        self, arguments: list[str], cwd: Path, stdin: str = "", environment: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return run_script(self.script, arguments, cwd, environment or self.environment, stdin)

    def other_repository(self) -> Path:
        """A throwaway repository outside the hub, for a location variable to name."""
        other = self.root / "other repository"
        other.mkdir()
        self.git(other, "init", "-q")
        return other
