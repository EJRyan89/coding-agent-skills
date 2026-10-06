"""Resolve a repository's reviewer from its configuration, and inspect the review skill it names.

A repository reviewer is configured in one of three ways:

    manifest_path   a manifest committed to the repository (read from the trusted commit)
    skill+manifest  the repository's review skill, run through a manifest kept outside the repository
    skill           the repository's review skill run as one entrypoint reviewer, which is correct only when
                    the skill does not start subagents of its own (a subagent cannot start subagents)

Inspection is deterministic: it reads the skill's frontmatter for the tools it may use and its text for
explicit signs that it starts subagents, and reports the evidence rather than guessing. A repository agent's name is
such a sign wherever it appears as a whole word, unless the name is a plain word (`review`, `Security`): then it
counts only where the text names an agent, in a code span, followed by "agent", or on a line that also delegates.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from review_config import default_manifest_path
from review_runtime import (
    ADAPTER_PROTOCOL_VERSION,
    Runner,
    RuntimeContractError,
    _read_git_file,
    _run_git,
    has_undecodable,
    load_manifest_from_commit,
    load_manifest_from_file,
    subprocess_runner,
    validate_adapter_manifest,
)

DELEGATION_TOOLS = {"agent", "task"}
DELEGATION_TEXT = re.compile(
    r"\bsub-?agents?\b|\bsubagent_type\b|\b(?:Agent|Task) tool\b|\bspawn(?:s|ed|ing)?\b", re.IGNORECASE
)
# Wording that, on the same line, makes a plain-word agent name a reference to that agent.
DELEGATION_PHRASE = re.compile(
    r"\bdelegat(?:e|es|ed|ing|ion)\b|\b(?:start|launch|run|dispatch|invoke)\w*\b.*\bagents?\b", re.IGNORECASE
)
AGENT_WORD = re.compile(r"\s+(?:sub-?)?agents?\b", re.IGNORECASE)
CODE_SPAN = re.compile(r"`([^`]*)`")
PLAIN_WORD = re.compile(r"[A-Za-z][a-z]*")
PATH_TOKEN = re.compile(r"`([^`\s]+)`|\]\(([^)\s]+)\)|(?<![\w./-])((?:[\w.-]+/)+[\w.-]+\.\w+)")
AGENT_DIRECTORIES = (".claude/agents/", ".github/agents/")


@dataclass
class Inspection:
    """What a review skill's own files say about how it runs."""

    skill: str
    commit: str
    tools: list[str] | None  # None: no tool list, so every tool (including delegation) is inherited
    delegates: str  # "yes" | "no" | "unknown"
    reason: str
    evidence: list[tuple[int, str]] = field(default_factory=list)
    references: list[str] = field(default_factory=list)


@dataclass
class ResolvedReviewer:
    manifest: dict[str, Any]
    source: str  # "repository-manifest" | "local-manifest" | "skill"
    location: str  # the manifest path (repository-relative or local) or the skill path
    local_root: Path | None = None
    inspection: Inspection | None = None


def _frontmatter(text: str) -> tuple[dict[str, list[str] | None], int]:
    """Tool lists from a leading `---` block: `tools` (agents) and `allowed-tools` (skills).

    Handles `key: A, B`, `key: ["A", "B"]`, and an indented `- A` list. Returns the lists by key and the line
    number where the body starts.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, 1
    tools: dict[str, list[str] | None] = {}
    current: str | None = None
    for number, line in enumerate(lines[1:], start=2):
        if line.strip() == "---":
            return tools, number + 1
        match = re.match(r"^([A-Za-z][\w-]*)\s*:\s*(.*)$", line)
        if match:
            key, value = match.group(1).casefold(), match.group(2).strip()
            current = key if key in {"tools", "allowed-tools"} else None
            if current:
                tools[current] = [
                    item for item in (part.strip().strip("'\"") for part in value.strip("[]").split(",")) if item
                ] if value else []
        elif current and re.match(r"^\s+-\s+", line):
            tools[current].append(line.strip()[1:].strip().strip("'\""))
    return {}, 1


def frontmatter_value(text: str, key: str) -> str | None:
    """A single `key: value` from a leading `---` block, unquoted, or None when the block or key is absent.

    A block with no closing `---` is no frontmatter, as `_frontmatter` reads it.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    found: str | None = None
    for line in lines[1:]:
        if line.strip() == "---":
            return found or None
        match = re.match(r"^([A-Za-z][\w-]*)\s*:\s*(.*)$", line)
        if match and found is None and match.group(1).casefold() == key.casefold():
            found = match.group(2).strip().strip("'\"") or ""
    return None


def _tool_name(spec: str) -> str:
    return spec.split("(", 1)[0].strip().casefold()


def _names_agent(stem: str, line: str) -> bool:
    """Whether a line names the agent whose file stem this is.

    A plain word such as `review` is also ordinary prose, so it names the agent only in a code span, followed by
    "agent", or on a line that also delegates. Any other name (`db-review`, `SecReview`) is one wherever it appears.
    """
    word = re.compile(rf"(?<![\w-]){re.escape(stem)}(?![\w-])")
    if not word.search(line):
        return False
    if not PLAIN_WORD.fullmatch(stem):
        return True
    return (any(word.search(span) for span in CODE_SPAN.findall(line))
            or any(AGENT_WORD.match(line, match.end()) for match in word.finditer(line))
            or bool(DELEGATION_TEXT.search(line) or DELEGATION_PHRASE.search(line)))


def inspect_skill(skill: str, text: str, commit: str, repository_files: set[str]) -> Inspection:
    """Whether a review skill starts subagents, from its tool list and its text, with the evidence."""
    lists, body_start = _frontmatter(text)
    tools = lists.get("tools", lists.get("allowed-tools"))
    names = {_tool_name(item) for item in tools} if tools is not None else None
    can_delegate = names is None or "*" in names or bool(names & DELEGATION_TOOLS)
    own = PurePosixPath(skill)
    # Every file of a name: .claude/agents/ and .github/agents/ may both define it, and the skill needs each.
    agents: dict[str, list[str]] = {}
    for path in sorted(repository_files):
        if path.startswith(AGENT_DIRECTORIES) and path.endswith(".md") and PurePosixPath(path) != own:
            agents.setdefault(PurePosixPath(path).stem, []).append(path)
    evidence: list[tuple[int, str]] = []
    references: set[str] = set()
    for number, line in enumerate(text.splitlines()[body_start - 1:], start=body_start):
        named = [stem for stem in agents if _names_agent(stem, line)]
        if DELEGATION_TEXT.search(line) or named:
            evidence.append((number, " ".join(line.split())[:160]))
        for match in PATH_TOKEN.finditer(line):
            token = next(group for group in match.groups() if group).split("#", 1)[0]
            token = token.removeprefix("./")
            if token in repository_files and token != skill:
                references.add(token)
        references.update(path for stem in named for path in agents[stem])
    if not can_delegate:
        delegates, reason = "no", "its tool list grants neither Agent nor Task"
    elif evidence:
        delegates, reason = "yes", "it may start subagents and its text says it does"
    elif names is not None:
        delegates, reason = "unknown", "its tool list grants Agent or Task, but its text never says it starts one"
    else:
        delegates, reason = "no", "its text never mentions starting subagents"
    return Inspection(skill, commit, tools, delegates, reason, evidence if can_delegate else [], sorted(references))


def repository_files(checkout: Path, commit: str, runner: Runner = subprocess_runner) -> set[str]:
    """Every path in the commit's tree that is UTF-8.

    A path that is not can never be a reviewer file: the configuration, manifests, and skill text that name reviewer
    files are UTF-8, so nothing can name it. It is left out rather than failing the review over an unrelated file.
    """
    listing = _run_git(checkout, runner, "ls-tree", "-r", "--name-only", "-z", commit)
    return {name for name in listing.split("\0") if name and not has_undecodable(name)}


def inspect_configured_skill(
    checkout: Path, commit: str, skill: str, runner: Runner = subprocess_runner
) -> Inspection:
    files = repository_files(checkout, commit, runner)
    if skill not in files:
        raise RuntimeContractError(f"The configured review skill {skill} does not exist at {commit[:12]}")
    try:
        text = _read_git_file(checkout, commit, skill, runner).decode("utf-8-sig")
    except UnicodeError as exc:
        raise RuntimeContractError(f"The review skill {skill} is not UTF-8 text") from exc
    return inspect_skill(skill, text, commit, files)


def entrypoint_manifest(reviewer_id: str, inspection: Inspection) -> dict[str, Any]:
    """Run a review skill that starts no subagents as one entrypoint reviewer, carrying the files it names."""
    return validate_adapter_manifest({
        "schema_version": 1,
        "id": reviewer_id,
        "protocol_version": ADAPTER_PROTOCOL_VERSION,
        "supports": ["initial", "re-review"],
        "required_capabilities": ["read-diff", "write-result"],
        "entrypoint": inspection.skill,
        "resources": inspection.references,
        "agent_profiles": [],
    })


def manifest_location(reviewer: dict[str, Any], config_path: Path, repository: str) -> Path | None:
    """The local manifest a reviewer names: an absolute path, or `true` for the default beside the config."""
    if reviewer.get("manifest") is True:
        return default_manifest_path(config_path, repository)
    if isinstance(reviewer.get("manifest"), str):
        return Path(reviewer["manifest"])
    return None


def resolve_reviewer(
    reviewer: dict[str, Any],
    *,
    checkout: Path,
    commit: str,
    config_path: Path,
    repository: str,
    runner: Runner = subprocess_runner,
) -> ResolvedReviewer:
    """The manifest a repository reviewer runs with, from whichever of the three sources its config names.

    A skill that starts subagents is never run as an entrypoint: the nested subagents it starts would fail,
    so it needs a specialists manifest that routes its specialists directly.
    """
    if reviewer.get("manifest_path"):
        manifest = load_manifest_from_commit(checkout, commit, reviewer["manifest_path"], runner=runner)
        return ResolvedReviewer(manifest, "repository-manifest", reviewer["manifest_path"])
    inspection = inspect_configured_skill(checkout, commit, reviewer["skill"], runner)
    local = manifest_location(reviewer, config_path, repository)
    if local is not None:
        return ResolvedReviewer(load_manifest_from_file(local), "local-manifest", str(local), local.parent,
                                inspection)
    if inspection.delegates == "yes":
        line = f" (line {inspection.evidence[0][0]}: {inspection.evidence[0][1]})" if inspection.evidence else ""
        raise RuntimeContractError(
            f"The review skill {inspection.skill} starts its own subagents{line}, which fails when it runs as a "
            "reviewer subagent. Give it a specialists manifest (reviewer.manifest); see inspect-reviewer."
        )
    return ResolvedReviewer(entrypoint_manifest(reviewer["id"], inspection), "skill", inspection.skill,
                            inspection=inspection)
