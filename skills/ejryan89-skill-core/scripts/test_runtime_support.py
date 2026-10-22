"""The runtime catalogue and the reader of a skill's runtime_support declaration (runtime_support.py)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import runtime_support

PARTIAL = {"level": "partial", "needs": ["sandbox-network"], "reason": "Its gh calls need the network."}


def test_a_declaration_reads_full_and_partial_runtimes() -> None:
    support = runtime_support.parse({"claude-code": "full", "codex": PARTIAL, "copilot-cli": "full"})
    assert support["claude-code"] == runtime_support.Support("full")
    assert support["codex"] == runtime_support.Support(
        "partial", ("sandbox-network",), "Its gh calls need the network."
    )


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ([], "must be an object keyed by runtime"),
        ({"claude-code": "full", "codex": "full"}, "does not declare copilot-cli"),
        ({"claude-code": "full", "codex": "full", "copilot-cli": "full", "other": "full"}, "unknown runtime 'other'"),
        ({"claude-code": "full", "codex": {**PARTIAL, "needs": []}, "copilot-cli": "full"}, "names no need"),
        ({"claude-code": "full", "codex": {**PARTIAL, "needs": ["flight"]}, "copilot-cli": "full"}, "'flight'"),
        ({"claude-code": "full", "codex": {**PARTIAL, "reason": "a\nb"}, "copilot-cli": "full"}, "one-line reason"),
    ],
)
def test_a_declaration_out_of_shape_names_its_problem(value: object, message: str) -> None:
    with pytest.raises(runtime_support.RuntimeSupportError, match=message):
        runtime_support.parse(value)


def test_the_needs_follow_from_grants_start_hooks_tools_and_git() -> None:
    assert runtime_support.frontmatter_needs(["Agent", "Read"], False) == {"agent-delegation"}
    assert runtime_support.frontmatter_needs(["Read"], False, starts_hooked_agent=True) == set()
    assert runtime_support.frontmatter_needs(["Agent"], True, starts_hooked_agent=True) == {
        "agent-delegation",
        "fail-closed-agent-hooks",
        "user-only-start",
    }
    assert runtime_support.frontmatter_needs([], False, tools=frozenset({"pdftotext", "gh"})) == {"sandbox-network"}
    assert runtime_support.frontmatter_needs([], False, git_subcommands=frozenset({"status"})) == set()
    assert runtime_support.frontmatter_needs([], False, git_subcommands=frozenset({"fetch"})) == {"sandbox-network"}


def test_what_a_runtime_lacks_decides_its_declaration() -> None:
    needs = {"workflow", "sandbox-network"}
    assert runtime_support.lacking("claude-code", needs) == ()
    assert runtime_support.lacking("codex", needs) == ("workflow", "sandbox-network")
    assert runtime_support.declaration(needs)["copilot-cli"] == {
        "level": "partial",
        "needs": ["workflow"],
        "reason": runtime_support.DEFAULT_REASONS["workflow"],
    }
