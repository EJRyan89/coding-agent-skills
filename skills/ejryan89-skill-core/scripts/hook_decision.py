"""The one writer of a PreToolUse hook's denial, the line a guard prints so Claude Code refuses the call.

A guard that ends in an error instead of a denial lets the call through, so the denial is always written. A reason
JSON cannot carry, such as one naming a path whose name decoded to a lone surrogate, which `json_writer` refuses,
gives way to the fixed reason the guard states for that case.
"""

from __future__ import annotations

import json_writer


def denial(reason: str, fallback: str) -> str:
    """The hook output that denies the call for `reason`, or for `fallback`, a fixed reason JSON can carry, when
    `json_writer` refuses `reason`."""
    try:
        return _denial_text(reason)
    except json_writer.JsonWriteRefused:
        return _denial_text(fallback)


def _denial_text(reason: str) -> str:
    return json_writer.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    )
