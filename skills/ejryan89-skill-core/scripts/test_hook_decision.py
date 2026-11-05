"""Regression tests for the one writer of a PreToolUse hook's denial."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hook_decision import denial

FALLBACK = "The guard denied this call for a reason JSON cannot carry."


class DenialTests(unittest.TestCase):
    def test_a_denial_is_this_line_with_text_past_ascii_escaped(self) -> None:
        self.assertEqual(
            '{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", '
            '"permissionDecisionReason": "C:/repo \\u2192 \\u2713/x.cs is outside it."}}',
            denial("C:/repo → ✓/x.cs is outside it.", FALLBACK),
        )

    def test_a_reason_json_cannot_carry_gives_way_to_the_fallback(self) -> None:
        self.assertEqual(
            '{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", '
            '"permissionDecisionReason": "The guard denied this call for a reason JSON cannot carry."}}',
            denial("/outside/x\udcff is outside it.", FALLBACK),
        )
