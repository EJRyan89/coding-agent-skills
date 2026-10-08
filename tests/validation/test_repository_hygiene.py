"""Fixture tests for tests/validation/repository_hygiene.py.

Each policy fails on a violating input and passes on a conforming one.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from repository_hygiene import hub_guard_matcher_problems, private_references


class RepositoryHygieneFixtures(unittest.TestCase):
    def test_hub_guard_matcher_policy_detects_a_missing_tool_or_hook(self) -> None:
        command = 'python -B "$CLAUDE_PROJECT_DIR/tools/worktrees.py" guard'

        def settings(matcher: str, hook_command: str = command) -> dict[str, object]:
            group = {"matcher": matcher, "hooks": [{"type": "command", "command": hook_command}]}
            return {"hooks": {"PreToolUse": [group]}}

        self.assertEqual([], hub_guard_matcher_problems(settings("Edit|Write|MultiEdit|NotebookEdit|Bash|PowerShell")))
        self.assertEqual([], hub_guard_matcher_problems(settings("*")))
        # The tracked command checks the opt-in before starting Python; the guard it runs is still the one matched.
        gated = '[ "$(git config --type=bool --get coding-agent-skills.hubGuard)" = true ] || exit 0; ' + command
        self.assertEqual(
            [], hub_guard_matcher_problems(settings("Edit|Write|MultiEdit|NotebookEdit|Bash|PowerShell", gated))
        )
        self.assertEqual(
            ["the hub guard's hook does not match the PowerShell tool"],
            hub_guard_matcher_problems(settings("Edit|Write|MultiEdit|NotebookEdit|Bash")),
        )
        self.assertEqual(
            ["the hub guard's hook does not match the Bash tool", "the hub guard's hook does not match the Write tool"],
            hub_guard_matcher_problems(settings("Edit|MultiEdit|NotebookEdit|PowerShell")),
        )
        broken_settings: list[dict[str, object]] = [
            settings("Bash|PowerShell", "python -B tools/other.py guard"),
            {},
            {"hooks": {}},
        ]
        for broken in broken_settings:
            with self.subTest(settings=broken):
                self.assertEqual(
                    ["no PreToolUse hook runs tools/worktrees.py guard"], hub_guard_matcher_problems(broken)
                )

    def test_private_reference_scan_detects_each_pattern(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            # Each sample is split so this source file does not match the scan it tests.
            user = "C:\\" + "Users\\someone\\GitHub"
            forward = "c:/" + "users/Someone/tools"
            synced = "One" + "Drive - Contoso"
            internal = "github.com/contoso" + "-internal/tools"
            samples = {
                "allowed.md": "C:/Users/YourName/GitHub and C:\\Users\\YourName\\scoop\n",
                "user.md": f"Install to {user}\n",
                "forward.md": f"see {forward}\n",
                "synced.md": f"C:/Data/{synced}/notes\n",
                "internal.md": f"https://{internal}\n",
                "public.md": "https://github.com/contoso/tools-internal-docs\n",
            }
            for name, content in samples.items():
                (root / name).write_text(content, encoding="utf-8")
            self.assertEqual(
                [
                    f"forward.md:1: see {forward}",
                    f"internal.md:1: https://{internal}",
                    f"synced.md:1: C:/Data/{synced}/notes",
                    f"user.md:1: Install to {user}",
                ],
                private_references(root, [root / name for name in samples]),
            )


if __name__ == "__main__":
    unittest.main()
