"""Repository hygiene: no private machine or organization names, and the hub guard's hook sees every editing tool."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from validation_support import REPOSITORY_ROOT, repository_files

# Every Claude Code tool that can edit a file or run git, each of which the hub guard's hook must see.
HUB_GUARD_TOOLS = {"Bash", "Edit", "MultiEdit", "NotebookEdit", "PowerShell", "Write"}


# Defense in depth only: the private-name scan in docs/releasing.md runs before every release. YourName is the
# placeholder user documentation may show; the drive-sync folder name is split so this file does not match itself.
PRIVATE_REFERENCE = re.compile(
    "|".join(
        (
            r"C:[/\\]Users[/\\](?!YourName(?:[/\\]|$))",
            "One" + r"Drive - [^/\\\r\n]+",
            r"github\.com[/\\][A-Za-z0-9_.-]+-internal(?:[/\\]|$)",
        )
    ),
    re.IGNORECASE,
)


def private_references(root: Path, files: list[Path]) -> list[str]:
    """Report each line naming a real user profile, a synced drive folder, or an internal GitHub organization."""
    found: list[str] = []
    for path in sorted(files):
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for number, line in enumerate(lines, start=1):
            if PRIVATE_REFERENCE.search(line):
                found.append(f"{path.relative_to(root).as_posix()}:{number}: {line.strip()}")
    return found


def hub_guard_matcher_problems(settings: dict[str, object]) -> list[str]:
    """Report each tool that can edit a file or run git whose calls the hub guard's PreToolUse hook would not see."""
    hooks = settings.get("hooks")
    groups = hooks.get("PreToolUse", []) if isinstance(hooks, dict) else []
    guarded = [
        group
        for group in groups
        if any(re.search(r"tools/worktrees\.py\"? guard$", hook.get("command", "")) for hook in group.get("hooks", []))
    ]
    if not guarded:
        return ["no PreToolUse hook runs tools/worktrees.py guard"]
    matched: set[str] = set()
    for group in guarded:
        matcher = group.get("matcher", "")
        if matcher in {"", "*"}:
            return []
        matched.update(matcher.split("|"))
    return [f"the hub guard's hook does not match the {tool} tool" for tool in sorted(HUB_GUARD_TOOLS - matched)]


class RepositoryHygienePolicies(unittest.TestCase):
    def test_tracked_claude_settings_hold_hooks_and_attribution_only(self) -> None:
        # Tracked settings reach every developer's sessions, so they may add hooks and set the repository's
        # commit and pull request attribution but never decide what a developer allows; permissions and every
        # other setting stay in user or local settings.
        settings = json.loads((REPOSITORY_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
        self.assertLessEqual(set(settings), {"$schema", "hooks", "attribution"})
        self.assertEqual([], [path for path in repository_files(REPOSITORY_ROOT) if path.name == "settings.local.json"])

    def test_hub_guard_hook_sees_every_tool_that_edits_files_or_runs_git(self) -> None:
        # A tool missing from the matcher is never shown to the guard, so the hub is open through it.
        settings = json.loads((REPOSITORY_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual([], hub_guard_matcher_problems(settings))

    def test_repository_has_no_private_machine_or_organization_references(self) -> None:
        self.assertEqual([], private_references(REPOSITORY_ROOT, repository_files(REPOSITORY_ROOT)))
