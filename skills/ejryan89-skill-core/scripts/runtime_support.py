"""Which agent runtimes run a skill: the capabilities each runtime offers and the reader of a skill's declaration.

A skill's `deploy-meta/<name>.json` may declare `runtime_support`, one value per runtime in RUNTIMES: `"full"`, or
an object whose `level` is `partial` or `none`, whose `reason` says in one line what the user loses, and whose `needs`
names the capabilities from CAPABILITIES the runtime lacks for it. Repository validation derives each skill's needs
from its frontmatter, with those of each skill it invokes, and holds the declaration to them;
tools/skill_reference.py prints the matrix and tools/runtime_canary.py checks it against what each runtime ran. It
lives in ejryan89-skill-core because audit-skill's script reads a declaration and derives the needs the same way,
through frontmatter_needs, invoked_skills, and carried_needs, wherever the skill it audits comes from, and a skill's
script imports nothing from the deployer.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection
from dataclasses import dataclass, field

RUNTIMES = ("claude-code", "codex", "copilot-cli")
RUNTIME_TITLES = {"claude-code": "Claude Code", "codex": "Codex CLI", "copilot-cli": "Copilot CLI"}
CAPABILITIES = {
    "agent-delegation": "starting a subagent from the session",
    "workflow": "Claude Code's Workflow tool, which starts subagents from a script with a per-call effort",
    "fail-closed-agent-hooks": (
        "running a subagent under its agent definition's hooks, with a tool call blocked when a hook fails or times "
        "out, which the code-review reviewer's guard needs"
    ),
    "user-only-start": "starting a user-only skill in a headless session as well as an interactive one",
    "sandbox-network": (
        "network access for a skill's commands in the runtime's default sandbox, which gh and a git fetch need"
    ),
}
# The allowed-tools grant that shows a skill needs a capability; user-only-start follows from disable-model-invocation,
# and fail-closed-agent-hooks from an Agent grant in a skill that starts an agent whose definition declares hooks.
CAPABILITY_GRANTS = {"Agent": "agent-delegation", "Workflow": "workflow"}
# The tools a skill may declare that reach a service over the network, so a skill declaring one, required or
# optional, needs sandbox-network.
NETWORK_TOOLS = frozenset({"gh", "copilot"})
# The git subcommands that reach a remote. Every skill runs git, so git is never declared as a tool; a skill whose
# script runs one of these needs sandbox-network as a network tool's does.
NETWORK_GIT_SUBCOMMANDS = frozenset({"clone", "fetch", "ls-remote", "pull", "push"})
# What a skill loses where a runtime lacks the capability, until its author states it more precisely.
DEFAULT_REASONS = {
    "agent-delegation": "It starts subagents, which this runtime cannot.",
    "workflow": "It uses Claude Code's Workflow tool, which this runtime lacks.",
    "fail-closed-agent-hooks": (
        "This runtime lets a tool call through when a hook times out, so its subagent runs without its agent "
        "definition's guard and its prompt is its only boundary."
    ),
    "user-only-start": "A headless copilot -p session cannot start it; start it from an interactive session.",
    "sandbox-network": "Its network calls need network access, which this runtime's default sandbox turns off.",
}
# Copilot CLI starts subagents, but its `copilot -p` neither expands /<skill> for a user-only skill nor lets the model
# start one (Copilot CLI 1.0.91; see docs/copilot-support.md). Workflow is Claude Code's alone. Codex and Copilot CLI
# run hooks, but Codex lets a tool call through when a hook fails or times out, and Copilot CLI when one times out,
# where Claude Code's agent definition blocks it, so a guard there holds only when it runs (the fail-closed decision in
# docs/design.md). Codex's default
# sandbox gives commands no network until [sandbox_workspace_write] network_access is set (docs/codex-support.md).
RUNTIME_CAPABILITIES = {
    "claude-code": frozenset(
        {"agent-delegation", "workflow", "fail-closed-agent-hooks", "user-only-start", "sandbox-network"}
    ),
    "codex": frozenset({"agent-delegation", "user-only-start"}),
    "copilot-cli": frozenset({"agent-delegation", "sandbox-network"}),
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


def _support(runtime: str, value: object) -> Support:
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


def parse(value: object) -> dict[str, Support]:
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


def frontmatter_needs(
    allowed_tools: list[str],
    user_only: bool,
    starts_hooked_agent: bool = False,
    tools: frozenset[str] = frozenset(),
    git_subcommands: frozenset[str] = frozenset(),
) -> set[str]:
    """The capabilities a skill's frontmatter, metadata, and scripts show it needs: its allowed-tools grants,
    user-only start, for a skill that grants Agent and starts an agent whose definition declares hooks, those hooks,
    and, for a skill that declares a network tool among its tools, required or optional, or whose scripts run a git
    subcommand that reaches a remote, network access."""
    needs = {CAPABILITY_GRANTS[grant] for grant in allowed_tools if grant in CAPABILITY_GRANTS}
    if starts_hooked_agent and "agent-delegation" in needs:
        needs.add("fail-closed-agent-hooks")
    if tools & NETWORK_TOOLS or git_subcommands & NETWORK_GIT_SUBCOMMANDS:
        needs.add("sandbox-network")
    return needs | {"user-only-start"} if user_only else needs


def invoked_skills(text: str, names: Collection[str]) -> set[str]:
    """The skills a SKILL.md's text invokes: each of `names` that is the first word of a code span on a line that
    says "invoke", such as `review-prs` in "invoke `review-prs --re-review`".
    tests/ai-config/test_cross_skill_contracts.py holds that no invoked skill is user-only."""
    span = re.compile(r"`([^`]+)`")
    return {
        words[0]
        for line in text.splitlines()
        if "invoke" in line
        for found in span.findall(line)
        if (words := found.split()) and words[0] in names
    }


def carried_needs(
    name: str,
    own_needs: Callable[[str], set[str]],
    invokes: Callable[[str], set[str]],
    seen: frozenset[str] = frozenset(),
) -> dict[str, set[str]]:
    """What each skill `name` invokes carries over to it: the invoked skill's own needs and what its own invoked skills
    carry, followed once each, without user-only-start, since starting an invoked skill is the invoker's step. A
    skill's needs are its own_needs and every set this returns for it; repository validation and audit-skill's script
    both derive them through it, each reading a skill's own needs and what it invokes from where the skill sits."""
    carried: dict[str, set[str]] = {}
    for other in sorted(invokes(name) - seen - {name}):
        needs = set(own_needs(other))
        for more in carried_needs(other, own_needs, invokes, seen | {name}).values():
            needs |= more
        carried[other] = needs - {"user-only-start"}
    return carried


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
