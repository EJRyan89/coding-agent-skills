"""Which agent runtimes run a skill: the capabilities each runtime offers and the reader of a skill's declaration.

A skill's `deploy-meta/<name>.json` may declare `runtime_support`, one value per runtime in RUNTIMES: `"full"`, or
an object whose `level` is `partial` or `none`, whose `reason` says in one line what the user loses, and whose `needs`
names the capabilities from CAPABILITIES the runtime lacks for it. Repository validation derives each skill's needs
from its frontmatter and holds the declaration to them; tools/skill_reference.py prints the matrix and
tools/runtime_canary.py checks it against what each runtime ran.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

RUNTIMES = ("claude-code", "codex", "copilot-cli")
RUNTIME_TITLES = {"claude-code": "Claude Code", "codex": "Codex CLI", "copilot-cli": "Copilot CLI"}
CAPABILITIES = {
    "agent-delegation": "starting a subagent from the session",
    "workflow": "Claude Code's Workflow tool, which starts subagents from a script with a per-call effort",
    "user-only-start": "starting a user-only skill in a headless session as well as an interactive one",
}
# The allowed-tools grant that shows a skill needs a capability; user-only-start follows from disable-model-invocation.
CAPABILITY_GRANTS = {"Agent": "agent-delegation", "Workflow": "workflow"}
# What a skill loses where a runtime lacks the capability, until its author states it more precisely.
DEFAULT_REASONS = {
    "agent-delegation": "It starts subagents, which this runtime cannot.",
    "workflow": "It uses Claude Code's Workflow tool, which this runtime lacks.",
    "user-only-start": "A headless copilot -p session cannot start it; start it from an interactive session.",
}
# Copilot CLI starts no subagent, and its `copilot -p` neither expands /<skill> for a user-only skill nor lets the model
# start one (Copilot CLI 1.0.91; see docs/copilot-support.md). Workflow is Claude Code's alone.
RUNTIME_CAPABILITIES = {
    "claude-code": frozenset({"agent-delegation", "workflow", "user-only-start"}),
    "codex": frozenset({"agent-delegation", "user-only-start"}),
    "copilot-cli": frozenset(),
}
FULL = "full"
PARTIAL = "partial"
NONE = "none"
SHAPE = (
    'runtime_support, where present, must name each of claude-code, codex, and copilot-cli once: either "full", '
    'or an object {"level": "partial" or "none", "needs": [capabilities], "reason": "<one line>"}, where a partial '
    "names at least one need. Known capabilities: " + ", ".join(CAPABILITIES) + '. See "Metadata" in '
    "docs/adding-a-skill.md."
)


class RuntimeSupportError(ValueError):
    """A runtime_support value that does not have the declared shape; the message says what is wrong."""


@dataclass(frozen=True)
class Support:
    level: str
    needs: tuple[str, ...] = field(default=())
    reason: str = ""


def _support(runtime: str, value: Any) -> Support:
    if value == FULL:
        return Support(FULL)
    if not isinstance(value, dict) or set(value) - {"level", "needs", "reason"}:
        raise RuntimeSupportError(f'{runtime} must be "full" or an object with level, needs, and reason')
    level, needs, reason = value.get("level"), value.get("needs", []), value.get("reason")
    if level not in (PARTIAL, NONE):
        raise RuntimeSupportError(f"{runtime} has level {level!r}; an object's level is partial or none")
    if not isinstance(needs, list) or not all(isinstance(need, str) for need in needs):
        raise RuntimeSupportError(f"{runtime} needs must be a list of capability names")
    unknown = [need for need in needs if need not in CAPABILITIES]
    if unknown:
        raise RuntimeSupportError(f"{runtime} needs unknown capability '{unknown[0]}'")
    if len(set(needs)) != len(needs):
        raise RuntimeSupportError(f"{runtime} names a need twice")
    if level == PARTIAL and not needs:
        raise RuntimeSupportError(f"{runtime} is partial but names no need")
    if not isinstance(reason, str) or not reason.strip() or "\n" in reason or "\r" in reason:
        raise RuntimeSupportError(f"{runtime} is {level} without a one-line reason")
    return Support(level, tuple(needs), reason.strip())


def parse(value: Any) -> dict[str, Support]:
    """Each runtime's support from a runtime_support value; raises RuntimeSupportError naming the first problem."""
    if not isinstance(value, dict):
        raise RuntimeSupportError("runtime_support must be an object keyed by runtime")
    unknown = sorted(set(value) - set(RUNTIMES))
    if unknown:
        raise RuntimeSupportError(f"runtime_support names unknown runtime '{unknown[0]}'")
    missing = [runtime for runtime in RUNTIMES if runtime not in value]
    if missing:
        raise RuntimeSupportError(f"runtime_support does not declare {missing[0]}")
    return {runtime: _support(runtime, value[runtime]) for runtime in RUNTIMES}


def frontmatter_needs(allowed_tools: list[str], user_only: bool) -> set[str]:
    """The capabilities a skill's own frontmatter shows it needs: its allowed-tools grants, and user-only start."""
    needs = {CAPABILITY_GRANTS[grant] for grant in allowed_tools if grant in CAPABILITY_GRANTS}
    return needs | {"user-only-start"} if user_only else needs


def lacking(runtime: str, needs: set[str]) -> tuple[str, ...]:
    """The capabilities among `needs` that `runtime` does not offer, in catalogue order."""
    return tuple(capability for capability in CAPABILITIES if capability in needs - RUNTIME_CAPABILITIES[runtime])


def declaration(needs: set[str]) -> dict[str, object]:
    """The runtime_support value that follows from `needs`, with the default reason for each partial."""
    value: dict[str, object] = {}
    for runtime in RUNTIMES:
        missing = lacking(runtime, needs)
        value[runtime] = (
            {"level": PARTIAL, "needs": list(missing), "reason": " ".join(DEFAULT_REASONS[need] for need in missing)}
            if missing
            else FULL
        )
    return value
