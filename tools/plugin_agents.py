"""An agent's definition as a Claude Code plugin carries it: the frontmatter without its hooks, and those hooks as
entries of the plugin's hooks file.

A plugin agent's own `hooks` are ignored, so a plugin runs them from `hooks/hooks.json`, for every matching tool call of
every session; a hook an agent declares must therefore hold to its own agent itself, as the code-review reviewer guard
does from the `agent_type` of each call. The hooks are read from the subset of YAML they are written in, block
mappings and sequences of scalars, and anything else is refused. A command names a script under the deployed skills
directory, which a plugin does not have, so each `os.path.expanduser('~/.claude/skills/<path>')` becomes the same path
under the plugin's root, which Claude Code exports to a plugin hook as CLAUDE_PLUGIN_ROOT; a command that names the
home's Claude folder any other way is refused.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "ejryan89-skill-core" / "scripts"))

import frontmatter

HOOKS_KEY = "hooks"
KEY_LINE = re.compile(r"^(?P<key>[A-Za-z][A-Za-z0-9_]*):(?:[ \t]+(?P<value>.*))?$")
DEPLOYED_SCRIPT = re.compile(r"os\.path\.expanduser\('~/\.claude/skills/(?P<path>[A-Za-z0-9_./-]+)'\)")
HOME_CLAUDE = re.compile(r"~/\.claude|\.claude[/\\]skills")

Node = str | int | list["Node"] | dict[str, "Node"]


class AgentError(ValueError):
    """An agent's hooks a plugin cannot carry as written."""


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _scalar(key: str, inline: str, following: list[str]) -> str | int:
    """A key's scalar value as frontmatter.py reads it, continuation lines included; a number of digits alone, as a
    timeout is, is an integer."""
    lines = [f"{key}: {inline}", *(f"  {line.strip()}" if line.strip() else "" for line in following)]
    try:
        value = frontmatter.Frontmatter(lines).string(key)
    except frontmatter.FrontmatterError as exc:
        raise AgentError(f"hooks: {exc}") from exc
    if value is None:
        raise AgentError(f"hooks: {key} has no value")
    return int(value) if value.isdigit() else value


def _node(lines: list[str], start: int, end: int) -> Node:
    """The block mapping or sequence lines[start:end] hold, all at the indentation of the first."""
    indent = _indent(lines[start])
    if lines[start].strip().startswith("- "):
        items: list[Node] = []
        heads = [index for index in range(start, end) if _indent(lines[index]) == indent]
        for number, head in enumerate(heads):
            if not lines[head].strip().startswith("- "):
                raise AgentError(f"hooks: a sequence item and a key share an indentation: {lines[head].strip()}")
            tail = heads[number + 1] if number + 1 < len(heads) else end
            # "- key: value" opens a mapping whose first key sits where its "-" was, two columns in.
            items.append(
                _node([*lines[:head], " " * (indent + 2) + lines[head].strip()[2:], *lines[head + 1 :]], head, tail)
            )
        return items
    mapping: dict[str, Node] = {}
    heads = [index for index in range(start, end) if _indent(lines[index]) == indent and lines[index].strip()]
    for number, head in enumerate(heads):
        tail = heads[number + 1] if number + 1 < len(heads) else end
        match = KEY_LINE.fullmatch(lines[head].strip())
        if match is None or match.group("key") in mapping:
            raise AgentError(f"hooks: not a key of its own: {lines[head].strip()}")
        inline = match.group("value")
        if inline:
            mapping[match.group("key")] = _scalar(match.group("key"), inline, lines[head + 1 : tail])
        elif head + 1 < tail and _indent(lines[head + 1]) > indent:
            mapping[match.group("key")] = _node(lines, head + 1, tail)
        else:
            raise AgentError(f"hooks: {match.group('key')} has no value")
    return mapping


def _plugin_command(command: str) -> str:
    """A hook's command as the plugin runs it: each deployed script under the plugin's root instead."""

    def plugin_path(match: re.Match[str]) -> str:
        parts = ", ".join(f"'{part}'" for part in match.group("path").split("/"))
        return f"os.path.join(os.environ['CLAUDE_PLUGIN_ROOT'], 'skills', {parts})"

    rewritten = DEPLOYED_SCRIPT.sub(plugin_path, command)
    if HOME_CLAUDE.search(rewritten):
        raise AgentError("hooks: a command names the home's Claude folder other than as a deployed script's path")
    return rewritten


def _commands(hooks: Node) -> Node:
    """`hooks` with every command rewritten for the plugin, refused unless it is a mapping of event names."""
    if not isinstance(hooks, dict):
        raise AgentError("hooks: not a mapping of event names")

    def rewrite(node: Node) -> Node:
        if isinstance(node, dict):
            return {
                key: _plugin_command(value) if key == "command" and isinstance(value, str) else rewrite(value)
                for key, value in node.items()
            }
        if isinstance(node, list):
            return [rewrite(item) for item in node]
        return node

    return rewrite(hooks)


def plugin_agent(text: str) -> tuple[str, dict[str, Node]]:
    """An agent definition without its hooks, and those hooks by event, with each command rewritten for the plugin;
    an agent without hooks comes back as it is, with none."""
    lines = text.split("\n")
    found = frontmatter.split(lines)
    if found is None or HOOKS_KEY not in frontmatter.Frontmatter(found[0]):
        return text, {}
    head, body = found
    start = next(index for index, line in enumerate(head) if line.startswith(f"{HOOKS_KEY}:"))
    end = next(
        (index for index in range(start + 1, len(head)) if head[index] and not head[index][0].isspace()), len(head)
    )
    if head[start].strip() != f"{HOOKS_KEY}:" or start + 1 == end:
        raise AgentError("hooks: not a block mapping of event names")
    hooks = _commands(_node(head, start + 1, end))
    if not isinstance(hooks, dict):
        raise AgentError("hooks: not a mapping of event names")
    kept = [lines[0], *head[:start], *head[end:], *lines[body - 1 :]]
    return "\n".join(kept), hooks
