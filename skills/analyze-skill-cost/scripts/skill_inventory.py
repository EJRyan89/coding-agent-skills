"""Deterministic measurements for the analyze-skill-cost audit.

    locate NAME [--repo DIR] [--home DIR]   find the skill's main file in the user, project, and source skill roots
    inventory DIR                           classify and size every file, estimate tokens, flag structure that
                                            departs from the published guidance, and size the Markdown files
                                            the skill reads from outside its folder
    tools FILE                              compare the tools a skill body references with its allowed-tools, and
                                            size the description every session loads
    scan FILE...                            list lines with cost cues for the agent to judge, and subagent
                                            prompts that a script writes at runtime

Every command prints one fact per line in forward-slash paths. locate prefers a source skill to its deployed
copy, and counts a .agents/skills shim that points to the .claude/skills skill of its name as that skill; it
exits 1 after NOT_FOUND or AMBIGUOUS. A missing or unreadable input prints FAILED <reason> as its last line and
exits 1; only a usage error exits 2.

Token estimate: ceil(prose_chars / 4 + code_chars / 3), in characters of the UTF-8 text. Every
character of a helper script or data file is code. In Markdown, lines inside fenced code blocks,
including the fence lines, are code and the rest is prose. A file that is not UTF-8 text counts 0.
The estimate is a relative signal, not the active model's tokenizer.

Structure: BODY_OVER_500_LINES <lines> counts SKILL.md lines after the frontmatter. DOC_NO_TOC <lines> <path> is
a Markdown file over 100 lines whose first heading below the title is not "Contents" or "Table of contents".
NESTED_REFERENCE <path:line> <reference> is a Markdown file other than SKILL.md that names another one in the
skill folder that SKILL.md itself never names, so it is reached only through another reference. A path resolves
from the naming file, or from the skill folder after ${CLAUDE_SKILL_DIR}/.

Listing: DESCRIPTION gives the description's characters and ceil(characters / 4) tokens, which every session
loads while the skill is model-invocable; INVOCATION is model, user-only (disable-model-invocation: true),
or hidden (also user-invocable: false); INTERNAL_LISTED marks internal wording in a model-invocable skill.

Tool references: a tool counts as used on a line when its exact, case-sensitive name is
  - the start of an inline code span (`Read`, `Grep -l`, `Glob("*")`), outside fenced blocks;
  - followed by "(" (call syntax) or by the word tool(s), call(s), or invocation(s); or
  - a multi-word tool name that is not an English word (AskUserQuestion, WebFetch, mcp__*), anywhere.
A bash, sh, shell, or PowerShell fence uses each of Bash and PowerShell that is allowed, else Bash: on
Windows, Claude Code may run a fence's command through either tool.
Sentence-initial verbs such as "Read the file" or lowercase "read and apply" never count as USED,
so they never produce MISSING_ALLOWED. They do produce IMPLIED for an allowed tool whose action the
prose names ("read", "search", "ask", "subagent", ...), which keeps it out of UNUSED_ALLOWED. A prohibition
names no use: an action a negation governs in its clause ("do not read", "never write, edit, or delete",
"without asking") implies nothing. Nor does a prompt quoted for a subagent after "prompt ...:" on a line that
names one: its verbs are the subagent's, so they neither use nor imply a tool.

Shell grants: in Claude Code, allowed-tools pre-approves its entries for the turn that starts the skill; it
restricts nothing. UNSCOPED_ALLOWED <entry> is a Bash or PowerShell entry that matches every command (bare,
(*), or (:*)). UNPAIRED_ALLOWED <entry> is a Bash pattern without the same PowerShell pattern, or the
reverse. UNGRANTED <tool> <line> <command> is a fence command that no pattern of a shell with patterns
matches, so running it through that tool prompts. A pattern matches the command's text, quotes included,
with * for any text. EXPANDS <line> <command> is a fence command that expands a shell variable or command
substitution other than ${CLAUDE_SKILL_DIR} or $ARGUMENTS, which prompts in both shells whatever is granted.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

import frontmatter
from console import use_utf8_output

MAIN_NAME = "skill.md"
HELPER_SUFFIXES = frozenset({".sh", ".bash", ".ps1", ".py", ".js", ".mjs", ".cjs", ".ts"})
CATEGORIES = ("main", "helper", "doc", "data")
# Structure limits from Anthropic's skill authoring best practices
# (https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices): "Keep SKILL.md body under
# 500 lines for optimal performance", and "For reference files longer than 100 lines, include a table of contents".
# They are counted in lines, as published; size in bytes is reported as data, never flagged.
BODY_LINE_LIMIT = 500
DOC_TOC_LINE_LIMIT = 100
SKIPPED_DIRECTORIES = frozenset({".git", "__pycache__", "node_modules"})
SKILL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
CODE_SPAN = re.compile(r"`([^`\n]+)`")
SHELL_FENCES = frozenset({"bash", "sh", "shell", "console"})
POWERSHELL_FENCES = frozenset({"powershell", "pwsh", "ps1"})
KNOWN_TOOLS = (
    "Agent",
    "AskUserQuestion",
    "Bash",
    "BashOutput",
    "Edit",
    "EnterWorktree",
    "ExitPlanMode",
    "ExitWorktree",
    "Glob",
    "Grep",
    "KillShell",
    "LS",
    "Monitor",
    "MultiEdit",
    "NotebookEdit",
    "NotebookRead",
    "PowerShell",
    "Read",
    "Skill",
    "SlashCommand",
    "Task",
    "TodoWrite",
    "WebFetch",
    "WebSearch",
    "Write",
)
# Tool names that are also English or technical words need a tool-use context to count.
CONTEXT_FREE_TOOLS = frozenset(
    {
        "AskUserQuestion",
        "BashOutput",
        "EnterWorktree",
        "ExitPlanMode",
        "ExitWorktree",
        "KillShell",
        "MultiEdit",
        "NotebookEdit",
        "NotebookRead",
        "SlashCommand",
        "TodoWrite",
        "WebFetch",
        "WebSearch",
    }
)
# Runtime-neutral prose names actions rather than tools. A matching verb in prose implies an allowed tool
# is needed, which keeps it out of UNUSED_ALLOWED, but never reports it as MISSING_ALLOWED.
IMPLIED_BY = {
    "Agent": re.compile(r"(?i)\b(?:subagents?|delegat(?:e|es|ed|ion))\b"),
    "AskUserQuestion": re.compile(r"(?i)\b(?:ask|asks|asking|question|questions)\b"),
    "Bash": re.compile(r"(?i)\b(?:run|runs|rerun|command|commands|script|scripts)\b"),
    "Edit": re.compile(r"(?i)\b(?:edit|edits|editing)\b"),
    "EnterWorktree": re.compile(r"(?i)\bworktrees?\b"),
    "Glob": re.compile(r"(?i)\b(?:glob|globs|list (?:the |every |all )?files)\b"),
    "Grep": re.compile(r"(?i)\b(?:search|searches|searching|grep)\b"),
    # Claude Code may run a command through PowerShell instead of Bash, so the same words imply either.
    "PowerShell": re.compile(r"(?i)\b(?:powershell|run|runs|rerun|command|commands|script|scripts)\b"),
    "Read": re.compile(r"(?i)\b(?:read|reads|reading)\b"),
    "Skill": re.compile(r"(?i)\b(?:invoke|invokes|invoking)\b"),
    "Write": re.compile(r"(?i)\b(?:write|writes|writing|create|creates)\b"),
}
# A prohibition names an action without needing its tool. A negator governs a later word in its clause when the word
# follows it directly ("do not read", "without reading") or opens an item of the list it heads ("do not import X,
# write Y, or read Z"). A clause ends at sentence punctuation, a dash, "but", or "instead"; a negator inside a
# subordinate clause ("If it is not, ask") reaches only that clause, which ends at a comma not followed by or/and/nor.
NEGATOR = re.compile(r"(?i)\b(?:not|never|without|cannot|\w+n't)\b")
CLAUSE_END = re.compile(r"(?i)[.;:!?](?=\s|$)|—|\s--\s|\b(?:but|instead)\b")
SUBORDINATOR = re.compile(
    r"(?i)\b(?:if|when|whenever|unless|once|because|since|while|although|though|until|after|before|where|whether)\b"
)
SUBORDINATE_END = re.compile(r"(?i),(?!\s*(?:or|and|nor)\b)")
GOVERNED_DIRECTLY = re.compile(r"(?i)\s*(?:to\s+)?")
GOVERNED_LIST_ITEM = re.compile(r"(?i)(?:,\s*(?:(?:or|and|nor)\s+)?|\s(?:or|and|nor)\s+)$")
# The prompt a skill hands a subagent, quoted after "prompt ...:" on a line that names a subagent. Its verbs are the
# subagent's actions, not the skill's, so they neither use nor imply a tool.
SUBAGENT_PROMPT = re.compile(r"(?i)\bprompts?\b[^`\"\n]*?:\s*(`[^`\n]+`|\"[^\"\n]+\")")
# Wording that marks a skill as internal; such a skill should not sit in the model's skill list.
INTERNAL_WORDING = re.compile(r"(?i)\binternal\b|not intended for direct invocation|do not invoke")
MCP_TOOL = re.compile(r"\bmcp__[A-Za-z0-9_-]+")
ALLOWED_ENTRY = re.compile(r"([A-Za-z_][\w-]*)(?:\([^)]*\))?")
ALLOWED_PATTERN = re.compile(r"([A-Za-z_][\w-]*)(?:\((.*)\))?", re.DOTALL)
SHELL_TOOLS = ("Bash", "PowerShell")
# Grant patterns that match every command, so they scope nothing.
UNSCOPED_PATTERNS = frozenset({"", "*", ":*"})
# A trailing && or | joins a fence's command to the next line, which Claude Code checks as its own command.
COMMAND_JOIN = re.compile(r"\s*(?:&&|\|\||\||\\)\s*$")
# Shell expansion other than the values Claude Code fills in before the command runs. A command holding one is
# never pre-approved, so it prompts in both shells whatever allowed-tools grants.
SHELL_EXPANSION = re.compile(r"(?<!\\)\$(?!\{CLAUDE_SKILL_DIR\}|ARGUMENTS\b)(?:[A-Za-z_{(])")
SHELL_FILE_COMMAND = r"(?:cat|head|tail|find|grep|rg|sed|awk|wc|ls)"
# Word edges that also treat hyphens as part of a word, so "rev-parse" is not "parse".
WORD_START = r"(?<![\w-])"
WORD_END = r"(?![\w-])"
CUES = (
    ("wide-glob", re.compile(r"\*\*/\*|\*\*[\\/]")),
    (
        "loop",
        re.compile(
            rf"(?i){WORD_START}(?:for each|for every|loops?|iterates?|iterating|repeat for|per[- ](?:file|item))"
            rf"{WORD_END}"
        ),
    ),
    (
        "read-then-search",
        re.compile(rf"(?i){WORD_START}read{WORD_END}.{{0,80}}{WORD_START}(?:search|grep|look for){WORD_END}"),
    ),
    (
        "generated-code",
        re.compile(
            rf"(?i)python3? -c{WORD_END}|<<-?\s*['\"]?[A-Z_]+['\"]?|"
            rf"{WORD_START}(?:write|writes|generate|generates|compose)"
            rf"{WORD_END}.{{0,40}}{WORD_START}(?:script|code|glue|program|one-liner){WORD_END}"
        ),
    ),
    (
        "per-item-command",
        re.compile(
            rf"(?i){WORD_START}(?:for each|for every|once per){WORD_END}.{{0,60}}{WORD_START}(?:run|runs|rerun)"
            rf"{WORD_END}|{WORD_START}(?:run|runs|rerun){WORD_END}.{{0,60}}{WORD_START}"
            rf"(?:for each|for every|once per){WORD_END}"
        ),
    ),
    (
        "relayed-output",
        re.compile(
            rf"(?i){WORD_START}(?:show|print|relay|display|present|pass on){WORD_END}.{{0,60}}"
            rf"(?:as-is|verbatim|exactly as (?:given|printed|shown)|unchanged){WORD_END}"
        ),
    ),
    (
        "rule-based-work",
        re.compile(
            rf"(?i){WORD_START}(?:parse|parses|parsing|normali[sz]e|normali[sz]es|extract|extracts|tally|dedupe|"
            rf"deduplicate|sort|sorts|compute|computes|calculate|calculates|convert|converts|reformat){WORD_END}"
        ),
    ),
)
# Inside code (fences and script files) only these cues apply; the rest describe prose.
CODE_CUES = frozenset({"wide-glob", "generated-code"})
# Lines that bound what a subagent sends back; without one, every reply lands in the orchestrator's context.
REPLY_BOUND = re.compile(r"(?i)\breply (?:with|only)\b|\brespond only\b|\breturn only\b|\bfinal (?:message|reply)\b")
DELEGATION = re.compile(r"(?i)\bsubagents?\b")
# A subagent told to read a prompt file: the real prompt is written at runtime and the audit cannot see it.
RUNTIME_PROMPT = re.compile(r"(?i)\bread <[^>]*\bprompt\b[^>]*>")
SCRIPT_PATH = re.compile(r"[\w./{}$-]*scripts/[\w.-]+\.(?:py|sh|bash|ps1|js|mjs)")
# A relative Markdown path that leaves the skill folder, such as `../shared.md`. With a ${CLAUDE_SKILL_DIR}
# prefix it resolves against the skill folder instead of the file that names it.
OUTSIDE_REFERENCE = re.compile(r"(?<![\w./-])(\$\{CLAUDE_SKILL_DIR\}/)?((?:\.\./)+[\w./-]+\.md)\b")
# A relative Markdown path that stays in the skill folder, such as `references/a.md` or `a.md`.
INSIDE_REFERENCE = re.compile(r"(?<![\w./$}-])(\$\{CLAUDE_SKILL_DIR\}/)?((?:\./)?[\w-][\w./-]*\.md)\b")
HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
CONTENTS_HEADING = re.compile(r"(?i)(?:table of )?contents")
FENCE_SHELL_COMMAND = re.compile(rf"(?:^|[|;&(]|\$\()\s*({SHELL_FILE_COMMAND})\b")
SPAN_SHELL_COMMAND = re.compile(rf"^{SHELL_FILE_COMMAND}(?:\s|$)")


class InputError(ValueError):
    pass


def posix(path: Path) -> str:
    return path.as_posix()


def read_text(path: Path) -> str | None:
    """Return a file's UTF-8 text, or None for binary or undecodable content."""
    data = path.read_bytes()
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


@dataclass
class Block:
    line: int
    language: str
    body: list[str] = field(default_factory=list)


def parse_fences(lines: list[str]) -> tuple[list[str | None], list[Block]]:
    """Return each line's fence language (None outside fences) and every fenced block."""
    languages: list[str | None] = [None] * len(lines)
    blocks: list[Block] = []
    opener = ""
    current: Block | None = None
    for index, line in enumerate(lines):
        if current is None:
            match = FENCE.match(line)
            if match and not (match.group(1)[0] == "`" and "`" in match.group(2)):
                opener = match.group(1)
                info = match.group(2).split()
                current = Block(index + 1, info[0].casefold() if info else "")
                languages[index] = current.language
            continue
        languages[index] = current.language
        stripped = line.strip()
        if stripped and set(stripped) == {opener[0]} and len(stripped) >= len(opener):
            blocks.append(current)
            current = None
        else:
            current.body.append(line)
    if current is not None:
        blocks.append(current)
    return languages, blocks


def body_start(lines: list[str]) -> int:
    """The index of a Markdown file's first body line: 0 when it has no frontmatter, or one that is not closed."""
    try:
        found = frontmatter.split(lines)
    except frontmatter.FrontmatterError:
        return 0
    return found[1] if found else 0


def parse_allowed_tools(value: str | list[str] | None) -> list[str] | None:
    """Base tool names from a JSON-style, comma- or space-separated, or block-list allowed-tools value."""
    if value is None:
        return None
    entries = value if isinstance(value, list) else [value]
    names: list[str] = []
    for entry in entries:
        for match in ALLOWED_ENTRY.finditer(entry.replace('"', " ").replace("'", " ")):
            if match.group(1) not in names:
                names.append(match.group(1))
    return names


def allowed_entries(value: str | list[str] | None) -> list[tuple[str, str | None]]:
    """Each allowed-tools entry as its tool name and its pattern, or None for a bare tool name."""
    if value is None:
        return []
    texts: list[str] = []
    for entry in value if isinstance(value, list) else [value]:
        whole = ALLOWED_PATTERN.fullmatch(entry.strip())
        texts += [entry.strip()] if whole else [match.group(0) for match in ALLOWED_ENTRY.finditer(entry)]
    entries: list[tuple[str, str | None]] = []
    for text in texts:
        match = ALLOWED_PATTERN.fullmatch(text)
        if match and (match.group(1), match.group(2)) not in entries:
            entries.append((match.group(1), match.group(2)))
    return entries


def entry_text(name: str, pattern: str | None) -> str:
    return name if pattern is None else f"{name}({pattern})"


def grants(pattern: str, command: str) -> bool:
    """Whether a Bash or PowerShell permission pattern matches a command, by Claude Code's rules.

    The comparison is on the command's text, quotes included, and `*` matches any text. The legacy `prefix:*` is
    read as `prefix *`. Where the rules are unclear this errs toward no match, so a grant is never overstated.
    """
    if pattern.endswith(":*"):
        pattern = pattern[:-2] + " *"
    expression = ".*".join(re.escape(part) for part in pattern.split("*"))
    return re.fullmatch(expression, command, re.DOTALL) is not None


def fence_commands(body: list[str], languages: list[str | None], start: int) -> list[tuple[int, str]]:
    """Each command line in a shell or PowerShell fence, with its file line number."""
    commands: list[tuple[int, str]] = []
    for offset, (line, language) in enumerate(zip(body, languages, strict=True)):
        if language not in SHELL_FENCES | POWERSHELL_FENCES or FENCE.match(line):
            continue
        command = COMMAND_JOIN.sub("", line.strip())
        if command and not command.startswith("#"):
            commands.append((start + offset + 1, command))
    return commands


def grant_findings(entries: list[tuple[str, str | None]], commands: list[tuple[int, str]]) -> list[str]:
    """Shell grants that scope nothing or lack their twin, and fence commands no grant of a shell covers."""
    shell = [(name, pattern) for name, pattern in entries if name in SHELL_TOOLS]
    output = [
        f"UNSCOPED_ALLOWED {entry_text(name, pattern)}"
        for name, pattern in shell
        if pattern is None or pattern.strip() in UNSCOPED_PATTERNS
    ]
    scoped = [
        (name, pattern) for name, pattern in shell if pattern is not None and pattern.strip() not in UNSCOPED_PATTERNS
    ]
    twin = {"Bash": "PowerShell", "PowerShell": "Bash"}
    output += [
        f"UNPAIRED_ALLOWED {entry_text(name, pattern)}"
        for name, pattern in scoped
        if (twin[name], pattern) not in shell
    ]
    for number, command in commands:
        for tool in SHELL_TOOLS:
            patterns = [pattern for name, pattern in shell if name == tool]
            if patterns and not any(
                pattern is None or pattern.strip() in UNSCOPED_PATTERNS or grants(pattern, command)
                for pattern in patterns
            ):
                output.append(f"UNGRANTED {tool} {number} {command}")
    output += [f"EXPANDS {number} {command}" for number, command in commands if SHELL_EXPANSION.search(command)]
    return output


# --- locate ---------------------------------------------------------------------------------------


def main_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.iterdir() if path.is_file() and path.name.casefold() == MAIN_NAME)


def skill_roots(home: Path, repo: Path | None) -> list[tuple[str, Path]]:
    roots = [("user", home / ".claude" / "skills")]
    if repo is not None:
        roots += [
            ("project-agents", repo / ".agents" / "skills"),
            ("project-claude", repo / ".claude" / "skills"),
            ("source", repo / "skills"),
        ]
    return roots


def skill_files(scope: str, directory: Path) -> list[Path]:
    """A skill directory's main files; a source skill also needs its deploy-meta, which shared assets lack."""
    if scope == "source" and not (directory.parent.parent / "deploy-meta" / f"{directory.name}.json").is_file():
        return []
    return main_files(directory)


def identity(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


def locate(name: str, home: Path, repo: Path | None) -> list[str]:
    if not SKILL_NAME.fullmatch(name):
        raise InputError(f"invalid skill name {name!r}")
    roots = skill_roots(home, repo)
    hits: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for scope, root in roots:
        for path in skill_files(scope, root / name):
            if identity(path) not in seen:
                seen.add(identity(path))
                hits.append((scope, path))
    # A developer in the source tree means the source, not the deployed copy of it.
    if any(scope == "source" for scope, _ in hits):
        hits = [(scope, path) for scope, path in hits if scope != "user"]
    # A .agents/skills shim that sends other runtimes to the .claude/skills skill of its name is that skill.
    if any(scope == "project-claude" for scope, _ in hits):
        pointer = f".claude/skills/{name}/SKILL.md"
        hits = [
            (scope, path)
            for scope, path in hits
            if not (scope == "project-agents" and pointer in (read_text(path) or ""))
        ]
    lines = [f"REPO {posix(repo)}" if repo is not None else "REPO none"]
    if len(hits) == 1:
        scope, path = hits[0]
        return [*lines, f"SKILL_FILE {posix(path)}", f"SKILL_DIR {posix(path.parent)}", f"SCOPE {scope}"]
    if hits:
        return [*lines, *(f"AMBIGUOUS {scope} {posix(path)}" for scope, path in hits)]
    lines.append(f"NOT_FOUND {name}")
    listed: set[str] = set()
    for scope, root in roots:
        if not root.is_dir():
            continue
        for directory in sorted(root.iterdir(), key=lambda path: path.name.casefold()):
            if directory.is_dir() and skill_files(scope, directory) and identity(directory) not in listed:
                listed.add(identity(directory))
                lines.append(f"AVAILABLE {scope} {directory.name}")
    return lines


def git_toplevel(directory: Path) -> Path | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=False
        )
    except OSError:
        return None
    output = completed.stdout.strip()
    return Path(output) if completed.returncode == 0 and output else None


# --- inventory ------------------------------------------------------------------------------------


def category(relative: Path) -> str:
    if len(relative.parts) == 1 and relative.name.casefold() == MAIN_NAME:
        return "main"
    suffix = relative.suffix.casefold()
    if suffix in HELPER_SUFFIXES:
        return "helper"
    return "doc" if suffix == ".md" else "data"


def estimate_tokens(text: str | None, kind: str) -> int:
    """ceil(prose / 4 + code / 3) in characters, where only Markdown has prose (outside fences)."""
    if text is None:
        return 0
    if kind in ("main", "doc"):
        lines = text.splitlines(keepends=True)
        languages, _ = parse_fences([line.rstrip("\r\n") for line in lines])
        code = sum(len(line) for line, language in zip(lines, languages, strict=True) if language is not None)
        prose = len(text) - code
    else:
        code, prose = len(text), 0
    return -(-(3 * prose + 4 * code) // 12)


def normalized_blocks(text: str) -> list[tuple[int, tuple[str, ...]]]:
    _, blocks = parse_fences(text.splitlines())
    result = []
    for block in blocks:
        body = tuple(line.strip() for line in block.body if line.strip())
        result.append((block.line, body))
    return result


def inventory(directory: Path) -> list[str]:
    if not directory.is_dir():
        raise InputError(f"skill directory not found: {directory}")
    files = sorted(
        (
            path
            for path in directory.rglob("*")
            if path.is_file() and not SKIPPED_DIRECTORIES.intersection(path.relative_to(directory).parts[:-1])
        ),
        key=lambda path: posix(path.relative_to(directory)).casefold(),
    )
    lines: list[str] = []
    totals = {name: [0, 0, 0] for name in CATEGORIES}
    flags: list[str] = []
    texts: dict[str, tuple[str, str]] = {}
    for path in files:
        relative = path.relative_to(directory)
        kind = category(relative)
        size = path.stat().st_size
        text = read_text(path)
        tokens = estimate_tokens(text, kind)
        lines.append(f"FILE {kind} {size} {tokens} {posix(relative)}")
        for index, value in enumerate((1, size, tokens)):
            totals[kind][index] += value
        if text is not None:
            texts[posix(relative)] = (kind, text)
        if text is not None:
            flags.extend(structure_flags(kind, text, posix(relative)))
    for kind in CATEGORIES:
        lines.append(f"TOTAL {kind} {totals[kind][0]} {totals[kind][1]} {totals[kind][2]}")
    lines.append("TOTAL all " + " ".join(str(sum(totals[kind][i] for kind in CATEGORIES)) for i in range(3)))
    if not totals["main"][0]:
        flags.insert(0, "NO_MAIN")
    return [
        *lines,
        *flags,
        *duplicate_flags(texts),
        *nested_flags(directory, texts),
        *outside_lines(directory, texts),
        *declared_lines(directory),
    ]


def structure_flags(kind: str, text: str, relative: str) -> list[str]:
    """A body past the published line limit, or a long reference file without a table of contents."""
    lines = text.splitlines()
    if kind == "main":
        body = len(lines) - body_start(lines)
        return [f"BODY_OVER_500_LINES {body}"] if body > BODY_LINE_LIMIT else []
    if kind != "doc" or len(lines) <= DOC_TOC_LINE_LIMIT:
        return []
    languages, _ = parse_fences(lines)
    for line, language in zip(lines, languages, strict=True):
        heading = HEADING.match(line) if language is None else None
        if heading and len(heading.group(1)) >= 2:
            if CONTENTS_HEADING.fullmatch(heading.group(2)):
                return []
            break
    return [f"DOC_NO_TOC {len(lines)} {relative}"]


def inside_references(root: Path, source: Path, lines: list[str]) -> list[tuple[int, str, Path]]:
    """Each Markdown path a file names inside the skill folder: line number, text, and the resolved file."""
    found = []
    for number, line in enumerate(lines, start=1):
        for match in INSIDE_REFERENCE.finditer(line):
            base = root if match.group(1) else source.parent
            found.append((number, match.group(0), (base / match.group(2)).resolve()))
    return found


def nested_flags(directory: Path, texts: dict[str, tuple[str, str]]) -> list[str]:
    """Reference files reached only through another reference file; published guidance links each from SKILL.md."""
    root = directory.resolve()
    docs = {(directory / relative).resolve() for relative, (kind, _) in texts.items() if kind == "doc"}
    linked: set[Path] = set()
    for relative, (kind, text) in texts.items():
        if kind == "main":
            lines = text.splitlines()
            linked.update(
                target for _, _, target in inside_references(root, root / relative, lines[body_start(lines) :])
            )
    flags: list[str] = []
    for relative, (kind, text) in texts.items():
        if kind != "doc":
            continue
        source = (directory / relative).resolve()
        for number, reference, target in inside_references(root, source, text.splitlines()):
            if target != source and target in docs and target not in linked:
                flags.append(f"NESTED_REFERENCE {relative}:{number} {reference}")
    return flags


def outside_lines(directory: Path, texts: dict[str, tuple[str, str]]) -> list[str]:
    """Markdown the skill's own Markdown points to outside its folder: a read, and its cost, on every run."""
    root = directory.resolve()
    lines: list[str] = []
    counted: dict[str, tuple[int, int]] = {}
    for relative, (kind, text) in texts.items():
        if kind not in ("main", "doc"):
            continue
        body = text.splitlines()
        start = body_start(body) if kind == "main" else 0
        for number in range(start, len(body)):
            for match in OUTSIDE_REFERENCE.finditer(body[number]):
                reference = match.group(0)
                base = directory if match.group(1) else (directory / relative).parent
                target = (base / match.group(2)).resolve()
                if target == root or root in target.parents:
                    continue
                where = f"{relative}:{number + 1}"
                if not target.is_file():
                    lines.append(f"OUTSIDE_MISSING {where} {reference}")
                    continue
                key = identity(target)
                if key not in counted:
                    counted[key] = (target.stat().st_size, estimate_tokens(read_text(target), "doc"))
                size, tokens = counted[key]
                lines.append(f"OUTSIDE_READ {where} {size} {tokens} {reference}")
    sizes = list(counted.values())
    lines.append(f"TOTAL outside {len(sizes)} {sum(size for size, _ in sizes)} {sum(tokens for _, tokens in sizes)}")
    return lines


def declared_lines(directory: Path) -> list[str]:
    """The skill's declared shared assets and skill dependencies, when it is audited from its source tree."""
    metadata = directory.parent.parent / "deploy-meta" / f"{directory.name}.json"
    if not metadata.is_file():
        return []
    try:
        declared = json.loads(metadata.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [f"DECLARED_UNREADABLE {posix(metadata)}"]
    return [
        *(f"DECLARED shared {name}" for name in declared.get("shared_deps", [])),
        *(f"DECLARED skill {name}" for name in declared.get("skill_deps", [])),
    ]


def duplicate_flags(texts: dict[str, tuple[str, str]]) -> list[str]:
    """Fenced blocks repeated in Markdown, and helper scripts copied into Markdown fences."""
    flags: list[str] = []
    first: dict[tuple[str, ...], str] = {}
    helpers = {
        path: "\n" + "\n".join(line.strip() for line in text.splitlines() if line.strip()) + "\n"
        for path, (kind, text) in texts.items()
        if kind == "helper"
    }
    for path, (kind, text) in texts.items():
        if kind not in ("main", "doc"):
            continue
        for line, body in normalized_blocks(text):
            if len(body) >= 2:
                if body in first:
                    flags.append(f"DUPLICATE_BLOCK {path}:{line} {first[body]}")
                else:
                    first[body] = f"{path}:{line}"
            if len(body) >= 3:
                needle = "\n" + "\n".join(body) + "\n"
                flags.extend(
                    f"INLINED_HELPER {path}:{line} {helper}" for helper, code in helpers.items() if needle in code
                )
    return flags


# --- tools ----------------------------------------------------------------------------------------


def line_references(line: str, language: str | None, names: list[str], allowed: list[str]) -> list[str]:
    """Tool names a single line uses, by the conservative rules in the module docstring."""
    found: list[str] = []
    if language in SHELL_FENCES | POWERSHELL_FENCES:
        return [name for name in SHELL_TOOLS if name in allowed] or ["Bash"]
    spans = [] if language is not None else [span.strip().strip("\"'") for span in CODE_SPAN.findall(line)]
    for name in names:
        escaped = re.escape(name)
        if (
            any(re.match(rf"{escaped}(?:$|[\s(])", span) for span in spans)
            or re.search(rf"(?<![\w-]){escaped}(?:\(|\s+(?:tools?|calls?|invocations?)\b)", line)
            or (
                (name in CONTEXT_FREE_TOOLS or name.startswith("mcp__"))
                and re.search(rf"(?<![\w-]){escaped}(?![\w-])", line)
            )
        ):
            found.append(name)
    return found


def blanked(text: str, start: int, end: int) -> str:
    return text[:start] + " " * (end - start) + text[end:]


def without_subagent_prompts(line: str) -> str:
    """The line with any prompt it hands a subagent blanked, keeping every other column in place."""
    if DELEGATION.search(line):
        for match in SUBAGENT_PROMPT.finditer(line):
            line = blanked(line, match.start(1), match.end(1))
    return line


def negated_ranges(line: str) -> list[tuple[int, int]]:
    """The column ranges each negator in the line reaches, by the rule above NEGATOR."""
    masked = line
    for match in CODE_SPAN.finditer(line):  # a dot in a code span ends no clause
        masked = blanked(masked, match.start() + 1, match.end() - 1)
    bounds = [0, *(match.end() for match in CLAUSE_END.finditer(masked)), len(masked)]
    ranges: list[tuple[int, int]] = []
    for start, end in pairwise(bounds):
        for negator in NEGATOR.finditer(masked, start, end):
            reach = end
            for subordinator in SUBORDINATOR.finditer(masked, start, negator.start()):
                closing = SUBORDINATE_END.search(masked, subordinator.end(), end)
                if (closing.start() if closing else end) > negator.start():
                    reach = closing.start() if closing else end
                    break
            ranges.append((negator.end(), reach))
    return ranges


def affirmed(pattern: re.Pattern[str], line: str) -> bool:
    """Whether the line names an action of the pattern that no negation governs."""
    ranges = negated_ranges(line)
    return any(
        not any(
            start <= match.start() < end
            and (
                GOVERNED_DIRECTLY.fullmatch(line[start : match.start()])
                or GOVERNED_LIST_ITEM.search(line[start : match.start()])
            )
            for start, end in ranges
        )
        for match in pattern.finditer(line)
    )


def tools(path: Path) -> list[str]:
    if not path.is_file():
        raise InputError(f"skill file not found: {path}")
    lines = (read_text(path) or "").splitlines()
    try:
        found = frontmatter.split(lines)
        document = frontmatter.Frontmatter(found[0] if found else [])
        model = document.string("model")
        allowed = parse_allowed_tools(document.value("allowed-tools"))
        entries = allowed_entries(document.value("allowed-tools"))
        listed = listing(document)
    except frontmatter.FrontmatterError as exc:
        raise InputError(f"{posix(path)}: {exc}") from exc
    start = found[1] if found else 0
    body = lines[start:]
    names = list(dict.fromkeys([*KNOWN_TOOLS, *(allowed or []), *MCP_TOOL.findall("\n".join(body))]))
    languages, _ = parse_fences(body)
    first_use: dict[str, int] = {}
    first_implied: dict[str, int] = {}
    for offset, (line, language) in enumerate(zip(body, languages, strict=True)):
        if language is None:
            line = without_subagent_prompts(line)
        for name in line_references(line, language, names, allowed or []):
            first_use.setdefault(name, start + offset + 1)
        if language is None:
            for name in allowed or []:
                if name in IMPLIED_BY and affirmed(IMPLIED_BY[name], line):
                    first_implied.setdefault(name, start + offset + 1)
    output = [f"MODEL {model or 'none'}", *listed]
    output += [f"ALLOWED {name}" for name in allowed] if allowed is not None else ["NO_ALLOWED_TOOLS"]
    used = sorted(first_use.items(), key=lambda item: (item[1], item[0]))
    output += [f"USED {name} {line}" for name, line in used]
    if allowed is not None:
        implied = [name for name in allowed if name in first_implied and name not in first_use]
        output += [f"IMPLIED {name} {first_implied[name]}" for name in implied]
        output += [f"UNUSED_ALLOWED {name}" for name in allowed if name not in first_use and name not in implied]
        output += [f"MISSING_ALLOWED {name} {line}" for name, line in used if name not in allowed]
        output += grant_findings(entries, fence_commands(body, languages, start))
    return output


def listing(document: frontmatter.Frontmatter) -> list[str]:
    """What every session loads for this skill: its description, unless model invocation is disabled."""
    text = (document.string("description") or "").strip()

    def flag(key: str) -> str:
        return (document.string(key) or "").strip().casefold()

    model_invocable = flag("disable-model-invocation") != "true"
    user_invocable = flag("user-invocable") != "false"
    invocation = "model" if model_invocable else ("user-only" if user_invocable else "hidden")
    lines = [f"DESCRIPTION {len(text)} {-(-len(text) // 4)}", f"INVOCATION {invocation}"]
    if model_invocable and INTERNAL_WORDING.search(text):
        lines.append("INTERNAL_LISTED")
    return lines


# --- scan -----------------------------------------------------------------------------------------


def scan(paths: list[Path]) -> list[str]:
    missing = [posix(path) for path in paths if not path.is_file()]
    if missing:
        raise InputError("file not found: " + ", ".join(missing))
    output: list[str] = []
    for path in paths:
        lines = (read_text(path) or "").splitlines()
        if path.suffix.casefold() == ".md":
            start = body_start(lines)
            languages, _ = parse_fences(lines)
        else:
            start, languages = 0, [path.suffix.casefold().lstrip(".") or "text"] * len(lines)
        for index in range(start, len(lines)):
            line, language = lines[index], languages[index]
            text = line.strip()
            if not text:
                continue
            cues = [name for name, pattern in CUES if pattern.search(line)]
            if language is not None:
                cues = [cue for cue in cues if cue in CODE_CUES]
            shell = (
                FENCE_SHELL_COMMAND.search(line)
                if language is not None
                else any(SPAN_SHELL_COMMAND.match(span.strip()) for span in CODE_SPAN.findall(line))
            )
            if shell:
                cues.append("shell-file-command")
            if language is None and line_references(line, None, ["Agent", "Task"], []):
                cues.append("agent")
            if "wide-glob" in cues and "head_limit" in line:
                cues.remove("wide-glob")
            excerpt = text if len(text) <= 160 else text[:157] + "..."
            output.extend(f"CUE {posix(path)} {index + 1} {cue} {excerpt}" for cue in cues)
        if path.suffix.casefold() == ".md":
            output.extend(delegation_lines(path, lines, languages, start))
    return output


def delegation_lines(path: Path, lines: list[str], languages: list[str | None], start: int) -> list[str]:
    """Delegations whose reply is never bounded, and subagent prompts that a script writes at runtime."""
    output: list[str] = []
    prose = [index for index in range(start, len(lines)) if languages[index] is None]
    delegations = [
        index
        for index in prose
        if DELEGATION.search(lines[index]) or line_references(lines[index], None, ["Agent", "Task"], [])
    ]
    if delegations and not any(REPLY_BOUND.search(lines[index]) for index in prose):
        text = lines[delegations[0]].strip()
        excerpt = text if len(text) <= 160 else text[:157] + "..."
        output.append(f"CUE {posix(path)} {delegations[0] + 1} subagent-reply {excerpt}")
    for index in prose:
        if not RUNTIME_PROMPT.search(lines[index]):
            continue
        writer = next(
            (
                match.group(0)
                for earlier in range(index - 1, -1, -1)
                if languages[earlier] is not None
                for match in [SCRIPT_PATH.search(lines[earlier])]
                if match
            ),
            "unknown",
        )
        output.append(f"RUNTIME_PROMPT {posix(path)} {index + 1} {writer}")
    return output


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    find = commands.add_parser("locate", help="find a skill's main file")
    find.add_argument("name")
    find.add_argument("--repo", type=Path, help="project root; default: the Git root of the current directory")
    find.add_argument("--home", type=Path, default=Path.home())
    commands.add_parser("inventory", help="size every file of a skill").add_argument("directory", type=Path)
    commands.add_parser("tools", help="compare referenced tools with allowed-tools").add_argument("file", type=Path)
    commands.add_parser("scan", help="list cost cues").add_argument("files", type=Path, nargs="+")
    args = parser.parse_args(arguments)
    try:
        if args.command == "locate":
            repo = args.repo if args.repo is not None else git_toplevel(Path.cwd())
            output = locate(args.name.strip(), args.home, repo)
        elif args.command == "inventory":
            output = inventory(args.directory)
        elif args.command == "tools":
            output = tools(args.file)
        else:
            output = scan(args.files)
    except InputError as exc:
        print(f"FAILED {exc}")
        return 1
    except OSError as exc:
        where = f" {Path(exc.filename).as_posix()}" if exc.filename else ""
        print(f"FAILED cannot read{where}: {exc.strerror or exc}")
        return 1
    print("\n".join(output))
    return 1 if any(line.startswith(("NOT_FOUND ", "AMBIGUOUS ")) for line in output) else 0


if __name__ == "__main__":
    use_utf8_output()
    raise SystemExit(main())
