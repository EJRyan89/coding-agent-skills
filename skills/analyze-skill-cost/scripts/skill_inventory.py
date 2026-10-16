"""Deterministic measurements for the analyze-skill-cost audit.

    locate NAME [--repo DIR] [--home DIR]   find the skill's main file in the user, project, and source skill roots
    inventory DIR                           classify and size every file, estimate tokens, flag structure that
                                            departs from the published guidance, and size the Markdown files
                                            the skill reads from outside its folder
    tools FILE                              compare the tools a skill body references with its allowed-tools, and
                                            size the description every session loads
    scan FILE...                            list lines with cost cues for the agent to judge, subagent prompts
                                            that a script writes at runtime, and the sample of each such prompt

Every command prints one fact per line in forward-slash paths. locate prefers a source skill to its deployed
copy, and counts a .agents/skills shim that points to the .claude/skills skill of its name as that skill; it
exits 1 after NOT_FOUND or AMBIGUOUS. A missing or unreadable input prints FAILED <reason> as its last line and
exits 1; only a usage error exits 2.

Token estimate: ceil(prose_chars / 4 + code_chars / 3), in characters of the UTF-8 text. Every
character of a helper script or data file is code. In Markdown, lines inside fenced code blocks,
including the fence lines, are code and the rest is prose. A helper script or data file that is not UTF-8 text
counts 0, since it is measured rather than loaded. A Markdown file the skill loads, its own or one it reads from
outside its folder, that is not UTF-8 text prints UNREADABLE <path> <reason> instead of a cost, the path relative to
the skill folder, and inventory exits 1 after it; tools and scan, which read the text itself, fail on one.
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
A fence labelled for a command shell (bash, sh, shell, console, zsh, ksh, dash, fish, cmd, bat, or batch) or for
PowerShell (powershell, pwsh, or ps1) uses each of Bash and PowerShell that is allowed, else Bash: on Windows, Claude
Code may run a fence's command through either tool. Fences are read as skill-core's find_fences reads them, so a
fence counts wherever Markdown allows one: indented, in a list item, or in a blockquote, with backticks or tildes.
Sentence-initial verbs such as "Read the file" or lowercase "read and apply" never count as USED,
so they never produce MISSING_ALLOWED. They do produce IMPLIED for an allowed tool whose action the
prose names ("read", "search", "ask", "subagent", ...), which keeps it out of UNUSED_ALLOWED. A prohibition
names no use: an action a negation governs in its clause ("do not read", "never write, edit, or delete",
"without asking") implies nothing. Nor does a prompt quoted for a subagent after "prompt ...:" on a line that
names one: its verbs are the subagent's, so they neither use nor imply a tool.

Shell grants: in Claude Code, allowed-tools pre-approves its entries for the turn that starts the skill; it
restricts nothing. UNSCOPED_ALLOWED <entry> is a Bash or PowerShell entry that matches every command (bare, (*), or
(:*)). UNPAIRED_ALLOWED <entry> is a Bash pattern without the same PowerShell pattern, or the reverse.
SIBLING_FOLDER_ALLOWED <entry> is a pattern that reaches a sibling skill through ${CLAUDE_SKILL_DIR}/../ without
naming one of its files, quoted, before any *: no path is normalized, so it matches every script of that skill and
every .. path out of it. OPEN_PATH_ALLOWED <entry> is any other pattern whose first * does not follow a space
outside quotes, such as scripts/* or tool.py*: the * continues the path before it, so the pattern matches every file
that path starts and every .. path out of it; a pattern ends a script's path at its closing quote, or at a space
when unquoted, before any *. NO_SCRIPT_ALLOWED <entry> is any other pattern with a * whose text before it names no
script: after the program, and capital-letter switches that take no value such as -B, no path ending in a script's
suffix, nor, for git or gh, a subcommand right after the program; so python *, bash -c *, or git * pre-approves any
code or subcommand the * supplies. A command is a fence command, or a code span outside a fence that runs a script:
a program and a script's path as that rule reads them. UNGRANTED <tool> <line> <command> is a command that no
pattern of a shell with patterns matches, so running it through that tool prompts. A pattern matches the command's
text, quotes included, with * for any text, and one whose only * ends it after a space also matches the bare command
before it. A line that joins commands with &&, ||, ;, or | outside quotes is judged one command at a time, as Claude
Code checks it, and UNGRANTED names the command no pattern matches. EXPANDS <line> <command> is a command line that
expands a shell variable or command substitution other than ${CLAUDE_SKILL_DIR} or $ARGUMENTS, which prompts in both
shells whatever is granted, so it is reported whether or not the skill has allowed-tools. IDLE_ALLOWED <entry> is a
scoped Bash or PowerShell pattern that matches no fence command and no code span outside a fence, so it pre-approves
a command the skill never asks for. HIDDEN_ALLOWED is a hidden skill with any grant: no turn starts it, so its
grants approve nothing, and it takes the place of its IDLE_ALLOWED lines.
A script that starts code the target repository configures declares a module-level RUNS_REPOSITORY_CODE string
saying so, and its command must keep prompting. A command that runs one, through ${CLAUDE_SKILL_DIR}, is
REPOSITORY_CODE <tool> <line> <command> when no grant of that tool covers it, as intended, and
GRANTED_REPOSITORY_CODE <tool> <line> <command> when one does; it is never UNGRANTED.

Runtime prompts: RUNTIME_PROMPT <file> <line> <writer> is a subagent told to read a prompt file that the named script
(or unknown) writes at runtime, which the skill's own files never show. A skill source tree keeps a rendered sample of
each such prompt as docs/runtime-prompts/<skill>/<name>.txt, naming the throwaway folder it was rendered in as <temp>
and the source tree as <source>. For each sample of the skill, scan prints RUNTIME_PROMPT_SAMPLE <sample> <tokens>,
one RUNTIME_PROMPT_READ <sample> <path> <tokens> per file of the source tree the prompt names other than a script,
which the subagent reads on every run, and REPLY_BOUNDED <sample> yes|no, whether the prompt bounds the reply. A skill
with no sample prints RUNTIME_PROMPT_UNSAMPLED <file> <line> <reason> for each runtime prompt instead.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from dataclasses import dataclass, field
from itertools import dropwhile, pairwise
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

import frontmatter
from console import run_main
from git_client import GitClient
from markdown_sections import CODE_SPAN, POWERSHELL_FENCES, SHELL_FENCES, find_fences, prose_code_spans

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
# Why a file the skill loads cannot be measured.
NOT_UTF8 = "not UTF-8 text"
SKILL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
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
ALLOWED_NAME = re.compile(r"[A-Za-z_][\w-]*")
SHELL_TOOLS = ("Bash", "PowerShell")
# Grant patterns that match every command, so they scope nothing.
UNSCOPED_PATTERNS = frozenset({"", "*", ":*"})
# How a command names a sibling skill's file. Claude Code normalizes no path in a shell grant, so a pattern that
# leaves the sibling's file open matches every script of that skill and every `..` path out of it.
SIBLING_PATH = "${CLAUDE_SKILL_DIR}/../"
# A trailing && or | joins a fence's command to the next line, which Claude Code checks as its own command.
COMMAND_JOIN = re.compile(r"\s*(?:&&|\|\||\||\\)\s*$")
# Shell expansion other than the values Claude Code fills in before the command runs. A command holding one is
# never pre-approved, so it prompts in both shells whatever allowed-tools grants.
SHELL_EXPANSION = re.compile(r"(?<!\\)\$(?!\{CLAUDE_SKILL_DIR\}|ARGUMENTS\b)(?:[A-Za-z_{(])")
# The module-level string a script declares when it starts code the target repository configures, and the script a
# fence command runs through the skill's directory.
REPOSITORY_CODE_DECLARATION = "RUNS_REPOSITORY_CODE"
SKILL_DIR_SCRIPT = re.compile(r"\$\{CLAUDE_SKILL_DIR\}/([^\"'\s]+\.py)")
# A command's words: a quoted string, or a run of other text up to a space or a quote.
COMMAND_WORD = re.compile(r"\"[^\"]*\"?|'[^']*'?|[^\s\"']+")
# A script's path, as a command or a grant names the file it runs.
SCRIPT_FILE = re.compile(
    rf"[^\s;&|<>()*`]+\.(?:{'|'.join(sorted(suffix[1:] for suffix in HELPER_SUFFIXES))})", re.IGNORECASE
)
# The switches an interpreter may take before the script it runs: capital-letter flags that take no value, such as
# Python's -B and -I. A lowercase switch such as -c, -e, or -m, a word such as -Command, or one that takes a value,
# such as -X, runs code, a module, or an option's value rather than the script.
SCRIPT_SWITCH = re.compile(r"-[BEIOPRS]+")
# The programs a grant may name by a subcommand, which must follow the program directly.
SUBCOMMAND_PROGRAMS = frozenset({"git", "gh"})
SUBCOMMAND = re.compile(r"[a-z][a-z0-9-]*")
SHELL_FILE_COMMAND = r"(?:cat|head|tail|find|grep|rg|sed|awk|wc|ls)"
# Word edges that also treat hyphens as part of a word, so "rev-parse" is not "parse".
WORD_START = r"(?<![\w-])"
WORD_END = r"(?![\w-])"
# An interpreter named as a command word, quoted or not, with or without a folder: the PYTHON template token, which a
# deployment renders as python or python3; python with any version or .exe; py, the Windows launcher; node; and
# PowerShell. inline_code reads the options after it.
INTERPRETER = re.compile(
    r"(?<![\w.{}$/\\-])[\"']?(?:[\w.:~/\\-]*[/\\])?"
    r"(\{\{PYTHON\}\}|python(?:\d+(?:\.\d+)*)?|py|node|pwsh|powershell)(?:\.exe)?[\"']?(?=\s|$|[`|;&)])",
    re.IGNORECASE,
)
# The words after a command's program up to the end of the command: a quoted string, a run of other text, or the
# operator, backtick, or parenthesis that ends it.
INTERPRETER_WORD = re.compile(r"[|;&`)]|\"[^\"]*\"?|'[^']*'?|[^\s\"'|;&`)]+")
# Python's options that take a value, as the next word when written alone, and the Windows launcher's version
# selectors, such as -3 or -3.11-64, which come before Python's own options.
PYTHON_VALUE_OPTIONS = frozenset({"X", "W"})
PYTHON_LONG_VALUE_OPTIONS = frozenset({"--check-hash-based-pycs"})
LAUNCHER_VERSION = re.compile(r"-(?:\d[\w.-]*|V:\S+)")
# Node's options that run code from the command line, and those that take the next word as a value.
NODE_INLINE_OPTIONS = frozenset({"--eval", "--print"})
NODE_VALUE_OPTIONS = frozenset({"-r", "--require", "--import", "--input-type"})
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
            rf"(?i)<<-?\s*['\"]?[A-Z_]+['\"]?|"
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
# Where a skill source tree keeps the rendered samples of its runtime prompts, one folder per skill.
RUNTIME_PROMPT_SAMPLES = Path("docs") / "runtime-prompts"
# A file of the source tree a rendered sample names, which the renderer wrote as <source> and the path below it.
SAMPLE_SOURCE_PATH = re.compile(r"<source>[\\/]([^\s\"'`<>]+)")
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


def require_text(path: Path) -> str:
    """A file's UTF-8 text; one that is binary or not UTF-8 fails rather than reading as empty."""
    text = read_text(path)
    if text is None:
        raise InputError(f"cannot read {posix(path)}: {NOT_UTF8}")
    return text


@dataclass
class Block:
    line: int
    language: str
    body: list[str] = field(default_factory=list)


def parse_fences(lines: list[str]) -> tuple[list[str | None], list[Block]]:
    """Return each line's fence language (None outside fences) and every fenced block, as skill-core's fence reader
    finds them, each body line without the blockquote markers that hold it."""
    languages: list[str | None] = [None] * len(lines)
    blocks: list[Block] = []
    for fence in find_fences(lines):
        language = fence.language.casefold()
        for index in fence.lines():
            languages[index] = language
        blocks.append(Block(fence.opener + 1, language, [fence.text(lines[index]) for index in fence.body()]))
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
    return list(dict.fromkeys(name for name, _ in allowed_entries(value)))


def split_entries(text: str) -> list[tuple[str, str | None]]:
    """The entries of one allowed-tools text: each tool name, and the pattern its parentheses hold.

    A pattern ends at the parenthesis that closes its opening one, so `Bash(a), PowerShell(a)` is two entries and
    `Bash(echo $(date))` one. Between entries, separators, quotes, and stray text are skipped; a pattern left open
    runs to the end of the text.
    """
    entries: list[tuple[str, str | None]] = []
    position = 0
    while (name := ALLOWED_NAME.search(text, position)) is not None:
        position = name.end()
        if not text.startswith("(", position):
            entries.append((name.group(0), None))
            continue
        depth = 0
        for index in range(position, len(text)):
            depth += {"(": 1, ")": -1}.get(text[index], 0)
            if depth == 0:
                entries.append((name.group(0), text[position + 1 : index]))
                position = index + 1
                break
        else:
            entries.append((name.group(0), text[position + 1 :]))
            position = len(text)
    return entries


def allowed_entries(value: str | list[str] | None) -> list[tuple[str, str | None]]:
    """Each allowed-tools entry as its tool name and its pattern, or None for a bare tool name."""
    if value is None:
        return []
    entries: list[tuple[str, str | None]] = []
    for text in value if isinstance(value, list) else [value]:
        entries += [entry for entry in split_entries(text) if entry not in entries]
    return entries


def entry_text(name: str, pattern: str | None) -> str:
    return name if pattern is None else f"{name}({pattern})"


def command_parts(command: str) -> list[str]:
    """The commands a command line joins with &&, ||, ;, or |, each stripped, which Claude Code checks against the
    grants one by one. An operator inside quotes, or escaped with a backslash outside single quotes, joins nothing."""
    parts: list[str] = []
    start, index, quote = 0, 0, ""
    while index < len(command):
        character = command[index]
        if character == "\\" and quote != "'":
            index += 2
            continue
        if quote:
            quote = "" if character == quote else quote
        elif character in "\"'":
            quote = character
        elif character in "&|;" and (character != "&" or command.startswith("&&", index)):
            parts.append(command[start:index])
            index += 2 if command.startswith(("&&", "||"), index) else 1
            start = index
            continue
        index += 1
    parts.append(command[start:])
    return [part.strip() for part in parts if part.strip()]


def grants(pattern: str, command: str) -> bool:
    """Whether a Bash or PowerShell permission pattern matches a command, by Claude Code's rules.

    The comparison is on the command's text, quotes included, and `*` matches any text. The legacy `prefix:*` is
    read as `prefix *`, and a pattern whose only `*` ends it after a space also matches the bare command before it.
    A command line that joins commands, as command_parts reads it, matches only when the pattern matches each of them.
    Where the rules are unclear this errs toward no match, so a grant is never overstated.
    """
    parts = command_parts(command)
    return bool(parts) and all(grants_one(pattern, part) for part in parts)


def grants_one(pattern: str, command: str) -> bool:
    """Whether a pattern matches one command, which joins no other."""
    pattern = trailing_wildcard(pattern)
    if pattern.endswith(" *") and pattern.count("*") == 1 and command == pattern[:-2]:
        return True
    expression = ".*".join(re.escape(part) for part in pattern.split("*"))
    return re.fullmatch(expression, command, re.DOTALL) is not None


def trailing_wildcard(pattern: str) -> str:
    """The pattern with the legacy `prefix:*` read as `prefix *`, which Claude Code treats alike."""
    return pattern[:-2] + " *" if pattern.endswith(":*") else pattern


def runs_repository_code(script: Path) -> bool:
    """Whether a Python script declares, as a module-level non-empty string, that it starts repository code."""
    text = read_text(script) if script.is_file() else None
    try:
        tree = ast.parse(text or "")
    except SyntaxError:
        return False
    for node in tree.body:
        targets = (
            node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        )
        value = node.value if isinstance(node, ast.Assign | ast.AnnAssign) else None
        if any(isinstance(target, ast.Name) and target.id == REPOSITORY_CODE_DECLARATION for target in targets):
            return isinstance(value, ast.Constant) and isinstance(value.value, str) and bool(value.value.strip())
    return False


def repository_code_commands(skill_dir: Path, commands: list[tuple[int, str]]) -> frozenset[str]:
    """The commands, each part of a joined line on its own, that run a script, through the skill's directory, that
    declares it starts repository code."""
    return frozenset(
        command
        for _, line in commands
        for command in command_parts(line)
        if any(runs_repository_code(skill_dir / script) for script in SKILL_DIR_SCRIPT.findall(command))
    )


def fence_commands(body: list[str], start: int) -> list[tuple[int, str]]:
    """Each command line in a shell or PowerShell fence, wherever it sits, with its file line number."""
    commands: list[tuple[int, str]] = []
    for fence in find_fences(body):
        if fence.language.casefold() not in SHELL_FENCES | POWERSHELL_FENCES:
            continue
        for index in fence.body():
            command = COMMAND_JOIN.sub("", fence.text(body[index]).strip())
            if command and not command.startswith("#"):
                commands.append((start + index + 1, command))
    return commands


def runs_a_script(words: list[str]) -> bool:
    """Whether a command's words run a script by its path: as the program, or after the program and the switches
    SCRIPT_SWITCH allows."""
    if not words:
        return False
    if SCRIPT_FILE.fullmatch(words[0]):
        return True
    rest = list(dropwhile(SCRIPT_SWITCH.fullmatch, words[1:]))
    return bool(rest) and SCRIPT_FILE.fullmatch(rest[0]) is not None


def command_words(text: str) -> list[str]:
    return [word.strip("\"'") for word in COMMAND_WORD.findall(text)]


def span_commands(body: list[str], start: int) -> list[tuple[int, str]]:
    """Each code span outside a fence in which a program runs a script, as a fence command does, with its file line
    number. A span that starts with the script's path, such as `deploy.py verify`, names the file or one of its
    commands rather than running it."""
    return [
        (start + index + 1, span.strip())
        for index, span in prose_code_spans(body)
        if (words := command_words(span)) and not SCRIPT_FILE.fullmatch(words[0]) and runs_a_script(words)
    ]


def names_what_it_runs(pattern: str) -> bool:
    """Whether a pattern's fixed part, the text before its first `*`, names what the command runs: a script by its
    path, as runs_a_script reads it, or a git or gh subcommand right after the program. A pattern with no `*` matches
    one command only, so it passes."""
    pattern = trailing_wildcard(pattern)
    if "*" not in pattern:
        return True
    words = command_words(pattern[: pattern.find("*")])
    if words and words[0] in SUBCOMMAND_PROGRAMS:
        return len(words) > 1 and SUBCOMMAND.fullmatch(words[1]) is not None
    return runs_a_script(words)


def names_one_sibling_script(pattern: str) -> bool:
    """Whether a pattern that reaches a sibling skill names one of its files, quoted, with no `*` before the quote
    closes, so no other script and no `..` path matches it. A pattern that reaches no sibling passes."""
    start = pattern.find(SIBLING_PATH)
    if start < 0:
        return True
    end = pattern.find('"', start)
    path = pattern[start:end]
    return pattern[start - 1 : start] == '"' and end >= 0 and "*" not in path and not path.endswith("/")


def ends_its_path_before_the_wildcard(pattern: str) -> bool:
    """Whether a pattern's first `*` follows a space outside quotes, so the path before it is closed and the `*`
    stands only for what follows the command's script: a `*` that continues a path or a quoted string matches every
    file that path starts and every `..` path out of it. A pattern with no `*` passes."""
    pattern = trailing_wildcard(pattern)
    star = pattern.find("*")
    if star < 0:
        return True
    return star > 0 and pattern[star - 1] == " " and pattern[:star].count('"') % 2 == 0


def grant_findings(
    entries: list[tuple[str, str | None]],
    commands: list[tuple[int, str]],
    repository_code: frozenset[str] = frozenset(),
) -> list[str]:
    """Shell grants that scope nothing, lack their twin, leave a path open before a `*`, or name no script before it,
    commands no grant of a shell covers, and whether a grant covers a command in repository_code, which runs code from
    the target repository and must keep prompting.
    """
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
    output += [
        f"SIBLING_FOLDER_ALLOWED {entry_text(name, pattern)}"
        for name, pattern in scoped
        if not names_one_sibling_script(pattern)
    ]
    output += [
        f"OPEN_PATH_ALLOWED {entry_text(name, pattern)}"
        for name, pattern in scoped
        if names_one_sibling_script(pattern) and not ends_its_path_before_the_wildcard(pattern)
    ]
    output += [
        f"NO_SCRIPT_ALLOWED {entry_text(name, pattern)}"
        for name, pattern in scoped
        if names_one_sibling_script(pattern)
        and ends_its_path_before_the_wildcard(pattern)
        and not names_what_it_runs(pattern)
    ]
    for number, line in commands:
        for tool in SHELL_TOOLS:
            patterns = [pattern for name, pattern in shell if name == tool]
            for command in command_parts(line):
                covered = any(
                    pattern is None or pattern.strip() in UNSCOPED_PATTERNS or grants(pattern, command)
                    for pattern in patterns
                )
                if command in repository_code:
                    output.append(
                        f"{'GRANTED_REPOSITORY_CODE' if covered else 'REPOSITORY_CODE'} {tool} {number} {command}"
                    )
                elif patterns and not covered:
                    output.append(f"UNGRANTED {tool} {number} {command}")
    output += [f"EXPANDS {number} {command}" for number, command in commands if SHELL_EXPANSION.search(command)]
    return output


def idle_findings(entries: list[tuple[str, str | None]], commands: list[str]) -> list[str]:
    """Scoped shell patterns that match none of the commands the skill shows, so they pre-approve one it never asks
    for. It is apart from grant_findings, which a scaffold calls before the skill shows any command.
    """
    return [
        f"IDLE_ALLOWED {entry_text(name, pattern)}"
        for name, pattern in entries
        if name in SHELL_TOOLS
        and pattern is not None
        and pattern.strip() not in UNSCOPED_PATTERNS
        and not any(grants(pattern, part) for command in commands for part in command_parts(command))
    ]


def shown_commands(body: list[str], start: int) -> list[str]:
    """Every command the body shows: each shell or PowerShell fence command, and each code span outside a fence."""
    commands = [command for _, command in fence_commands(body, start)]
    return commands + [span.strip() for _, span in prose_code_spans(body)]


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


def git_toplevel(directory: Path, git: GitClient | None = None) -> Path | None:
    """The root of the repository containing directory, or None outside one or when git cannot run or finish."""
    return (git or GitClient()).rev_parse_path("--show-toplevel", directory=directory)


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
    unreadable: list[str] = []
    for path in files:
        relative = path.relative_to(directory)
        kind = category(relative)
        size = path.stat().st_size
        text = read_text(path)
        if text is None and kind in ("main", "doc"):
            unreadable.append(f"UNREADABLE {posix(relative)} {NOT_UTF8}")
        tokens = estimate_tokens(text, kind)
        lines.append(f"FILE {kind} {size} {tokens} {posix(relative)}")
        for index, value in enumerate((1, size, tokens)):
            totals[kind][index] += value
        if text is not None:
            texts[posix(relative)] = (kind, text)
            flags.extend(structure_flags(kind, text, posix(relative)))
    for kind in CATEGORIES:
        lines.append(f"TOTAL {kind} {totals[kind][0]} {totals[kind][1]} {totals[kind][2]}")
    lines.append("TOTAL all " + " ".join(str(sum(totals[kind][i] for kind in CATEGORIES)) for i in range(3)))
    if not totals["main"][0]:
        flags.insert(0, "NO_MAIN")
    return [
        *lines,
        *unreadable,
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
    unreadable: dict[str, str] = {}
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
                if key in unreadable:
                    continue
                if key not in counted:
                    outside = read_text(target)
                    if outside is None:
                        unreadable[key] = f"UNREADABLE {outside_path(root, target)} {NOT_UTF8}"
                        continue
                    counted[key] = (target.stat().st_size, estimate_tokens(outside, "doc"))
                size, tokens = counted[key]
                lines.append(f"OUTSIDE_READ {where} {size} {tokens} {reference}")
    sizes = list(counted.values())
    lines.append(f"TOTAL outside {len(sizes)} {sum(size for size, _ in sizes)} {sum(tokens for _, tokens in sizes)}")
    return [*lines, *unreadable.values()]


def outside_path(root: Path, target: Path) -> str:
    """A file outside the skill folder, relative to the folder, or absolute on another drive."""
    try:
        return posix(Path(os.path.relpath(target, root)))
    except ValueError:
        return posix(target)


def source_root(directory: Path) -> Path | None:
    """The source tree a skill folder belongs to, which holds the skill's deploy metadata, or None."""
    root = directory.parent.parent
    return root if (root / "deploy-meta" / f"{directory.name}.json").is_file() else None


def declared_lines(directory: Path) -> list[str]:
    """The skill's declared shared assets and skill dependencies, when it is audited from its source tree."""
    root = source_root(directory)
    if root is None:
        return []
    metadata = root / "deploy-meta" / f"{directory.name}.json"
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
    lines = require_text(path).splitlines()
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
    # A command prompts or expands whether or not the skill grants anything, so its lines need no allowed-tools.
    commands = sorted(fence_commands(body, start) + span_commands(body, start))
    output += grant_findings(entries, commands, repository_code_commands(path.parent, commands))
    if allowed is not None:
        if "INVOCATION hidden" in listed:
            output += ["HIDDEN_ALLOWED"] if allowed else []
        else:
            output += idle_findings(entries, shown_commands(body, start))
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


def inline_code(line: str) -> bool:
    """Whether a line runs an interpreter on code written in the command rather than a script file: Python told -c or
    - (the program read from a pipe or a here-document), Node told -e or -p, or PowerShell told -Command or
    -EncodedCommand, each read by the options its program takes before the code."""
    for match in INTERPRETER.finditer(line):
        words: list[str] = []
        for word in INTERPRETER_WORD.findall(line, match.end()):
            if word in ("|", ";", "&", "`", ")"):
                break
            words.append(word.strip("\"'"))
        program = match.group(1).casefold()
        if program == "node":
            found = node_inline_code(words)
        elif program in ("pwsh", "powershell"):
            found = powershell_inline_code(words)
        else:
            found = python_inline_code(list(dropwhile(LAUNCHER_VERSION.fullmatch, words)) if program == "py" else words)
        if found:
            return True
    return False


def python_inline_code(words: list[str]) -> bool:
    """Whether Python's arguments run code they hold: -c, alone or ending a group of flags such as -Bc, or - for the
    program on standard input, before any script or -m."""
    rest = iter(words)
    for word in rest:
        if word == "-":
            return True
        if not word.startswith("-") or word == "--":
            return False
        if word.startswith("--"):
            if word in PYTHON_LONG_VALUE_OPTIONS:
                next(rest, None)
            continue
        for position, flag in enumerate(word[1:], start=1):
            if flag in "cm":
                return flag == "c"
            if flag in PYTHON_VALUE_OPTIONS:
                if position == len(word) - 1:
                    next(rest, None)
                break
    return False


def node_inline_code(words: list[str]) -> bool:
    """Whether Node's arguments run code they hold, by -e or -p, alone or in a group, or their long forms, or - for
    the program on standard input, before any script."""
    rest = iter(words)
    for word in rest:
        if word == "-" or word.split("=", 1)[0] in NODE_INLINE_OPTIONS:
            return True
        if not word.startswith("-") or word == "--":
            return False
        if word in NODE_VALUE_OPTIONS:
            next(rest, None)
        elif not word.startswith("--") and ("e" in word or "p" in word):
            return True
    return False


def powershell_inline_code(words: list[str]) -> bool:
    """Whether PowerShell's parameters run a command they hold: -Command or -EncodedCommand, by any prefix PowerShell
    reads as one, before any -File."""
    for word in words:
        name = word.casefold()[1:] if word.startswith("-") else ""
        if name == "f" or (len(name) >= 2 and "file".startswith(name)):
            return False
        if name in ("c", "e", "ec") or (len(name) >= 3 and "command".startswith(name)):
            return True
        if len(name) >= 2 and "encodedcommand".startswith(name):
            return True
    return False


def scan(paths: list[Path]) -> list[str]:
    missing = [posix(path) for path in paths if not path.is_file()]
    if missing:
        raise InputError("file not found: " + ", ".join(missing))
    texts = [require_text(path) for path in paths]
    output: list[str] = []
    runtime_prompts: list[tuple[Path, int]] = []
    for path, text in zip(paths, texts, strict=True):
        lines = text.splitlines()
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
            cues = [
                name
                for name, pattern in CUES
                if pattern.search(line) or (name == "generated-code" and inline_code(line))
            ]
            if language is not None:
                cues = [cue for cue in cues if cue in CODE_CUES]
            shell = (
                FENCE_SHELL_COMMAND.search(line)
                if language is not None
                else any(SPAN_SHELL_COMMAND.match(span.strip()) for span in CODE_SPAN.findall(line))
            )
            if shell:
                cues.append("shell-file-command")
            if language is None and (DELEGATION.search(line) or line_references(line, None, ["Agent", "Task"], [])):
                cues.append("agent")
            if "wide-glob" in cues and "head_limit" in line:
                cues.remove("wide-glob")
            excerpt = text if len(text) <= 160 else text[:157] + "..."
            output.extend(f"CUE {posix(path)} {index + 1} {cue} {excerpt}" for cue in cues)
        if path.suffix.casefold() == ".md":
            found = delegation_lines(path, lines, languages, start)
            output.extend(found)
            runtime_prompts += [(path, int(line.split()[2])) for line in found if line.startswith("RUNTIME_PROMPT ")]
    return output + sample_lines(runtime_prompts)


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


def skill_folder(path: Path) -> Path | None:
    """The skill folder a scanned file belongs to: the nearest folder above it that holds SKILL.md, named as the
    file is, so a relative path stays relative."""
    for folder in path.parents:
        folder = folder if folder.name else folder.resolve()
        if any(child.name.casefold() == MAIN_NAME for child in folder.iterdir() if child.is_file()):
            return folder
    return None


def sample_lines(runtime_prompts: list[tuple[Path, int]]) -> list[str]:
    """Measure the rendered sample of each runtime prompt once per skill, or say why a prompt has none."""
    output: list[str] = []
    measured: set[Path] = set()
    for path, line in runtime_prompts:
        folder = skill_folder(path)
        root = None if folder is None else source_root(folder)
        if folder is None or root is None:
            output.append(f"RUNTIME_PROMPT_UNSAMPLED {posix(path)} {line} not in a skill source tree")
            continue
        samples_folder = root / RUNTIME_PROMPT_SAMPLES / folder.name
        samples = sorted(samples_folder.glob("*.txt")) if samples_folder.is_dir() else []
        if not samples:
            output.append(f"RUNTIME_PROMPT_UNSAMPLED {posix(path)} {line} no sample under {posix(samples_folder)}")
        elif samples_folder not in measured:
            measured.add(samples_folder)
            for sample in samples:
                output.extend(measure_sample(root, sample))
    return output


def measure_sample(root: Path, sample: Path) -> list[str]:
    """A rendered prompt's estimate, the source files it has the subagent read, and whether it bounds the reply."""
    text = require_text(sample)
    output = [f"RUNTIME_PROMPT_SAMPLE {posix(sample)} {estimate_tokens(text, 'doc')}"]
    named = {match.group(1).replace("\\", "/").rstrip(".,;:)") for match in SAMPLE_SOURCE_PATH.finditer(text)}
    for relative in sorted(named):
        target = root / relative
        if target.suffix.casefold() not in HELPER_SUFFIXES and target.is_file():
            tokens = estimate_tokens(require_text(target), "doc")
            output.append(f"RUNTIME_PROMPT_READ {posix(sample)} {relative} {tokens}")
    output.append(f"REPLY_BOUNDED {posix(sample)} {'yes' if REPLY_BOUND.search(text) else 'no'}")
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
    return 1 if any(line.startswith(("NOT_FOUND ", "AMBIGUOUS ", "UNREADABLE ")) for line in output) else 0


if __name__ == "__main__":
    run_main(main)
