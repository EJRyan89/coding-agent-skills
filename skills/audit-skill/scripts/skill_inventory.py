"""Deterministic measurements for the audit-skill audit.

    locate [NAME] [--repo DIR] [--home DIR] find the skill's main file in the user, project, source, and plugin skill
                                            roots
    inventory DIR                          classify and size every file, estimate tokens, flag structure that
                                            departs from the published guidance, and size the Markdown files
                                            the skill reads from outside its folder
    tools FILE                              compare the tools a skill body references with its allowed-tools, and
                                            size the description every session loads
    provenance CHECK...                     name the guidance each check derives from, for a finding's line or cue
    scan PATH...                            list lines with cost cues for the agent to judge, subagent prompts
                                            that a script writes at runtime, and the sample of each such prompt;
                                            a skill folder stands for its SKILL.md and then every other Markdown
                                            file inventory lists, in its order, and also compares the status lines
                                            its Markdown names with what its scripts print

Every command prints one fact per line in forward-slash paths. locate prefers a source skill to its deployed
copy, and counts a .agents/skills shim that points to the .claude/skills skill of its name as that skill. It ends
NOT_FOUND and its AVAILABLE lines, or AMBIGUOUS lines, with STOP <message>, the message the audit stops with, and
prints only that line for an empty name; it exits 1 after STOP. A missing or unreadable input prints
FAILED <reason> as its last line and exits 1; only a usage error exits 2.

Plugins: a Claude Code plugin's root holds its manifest, its skills under skills/, its agents under agents/, and the
hooks it runs in every session while enabled in hooks/hooks.json, at the paths ejryan89-skill-core's skill_menu names.
locate's NAME may be <plugin>:<skill>, as Claude Code names a plugin's skill, and may be followed by a space and a
plugin folder: a plugin root, a folder of them as tools/plugin_build.py writes, or Claude Code's plugin cache, searched
down to PLUGIN_DEPTH folders. A plugin name or folder searches plugins alone; a bare name also searches the plugin cache
under the home, preferring any other copy to a plugin's. A plugin copy is SCOPE plugin, followed by
PLUGIN <name> <root>, the name from the plugin's manifest. inventory reports what the copy's plugin carries beside it,
CARRIED_BY <plugin> and PLUGIN_CARRIES skill|agent <name>, an agent by the namespaced name Claude Code gives it; in a
source tree that has a marketplace file, it reports CARRIED_BY for each plugin skill_menu plans to carry the skill, or,
when none does, LEFT_OUT <item> <reason> for each menu item that needs the skill and why no plugin carries it, or
PLUGINS_UNREADABLE <reason>. scan reads a plugin's agents by their namespaced names and prints
PLUGIN_HOOK <event> <matcher> <program> timeout=<seconds or default> on_failure=<value> for each command hook the
plugin's hooks file holds, a hook that counts as guarding every agent the plugin carries, since the build moves each
agent's own hooks there. Every other line measures the plugin's bytes as it measures a deployed copy's.

Token estimate: ceil(prose_chars / 4 + code_chars / 3), in characters of the UTF-8 text. Every
character of a helper script or data file is code. In Markdown, lines inside fenced code blocks,
including the fence lines, are code and the rest is prose. A helper script or data file that is not UTF-8 text
counts 0, since it is measured rather than loaded. A Markdown file the skill loads, its own or one it reads from
outside its folder, that is not UTF-8 text prints UNREADABLE <path> <reason> instead of a cost, the path relative to
the skill folder, and inventory exits 1 after it; tools and scan, which read the text itself, fail on one. scan fails
too on a script whose status lines it compares that is not UTF-8 text or not valid Python, whose output it cannot
measure.
The estimate is a relative signal, not the active model's tokenizer.

Structure: BODY_OVER_500_LINES <lines> counts SKILL.md lines after the frontmatter. DOC_NO_TOC <lines> <path> is
a Markdown file over 100 lines whose first heading below the title is not "Contents" or "Table of contents".
NESTED_REFERENCE <path:line> <reference> is a Markdown file other than SKILL.md that names another one in the
skill folder that SKILL.md itself never names, so it is reached only through another reference. A path resolves
from the naming file, or from the skill folder after ${CLAUDE_SKILL_DIR}/.

Listing: DESCRIPTION gives the description's characters and ceil(characters / 4) tokens, which every session loads while
the skill is model-invocable; INVOCATION is model, user-only (disable-model-invocation: true), or hidden (also
user-invocable: false); DESCRIPTION_LEAD <sentence> is a model-invocable description's first sentence, which a runtime
shortening its listing keeps longest; INTERNAL_LISTED marks internal wording in a model-invocable skill. For a
model-invocable skill in its source tree, TRIGGER_SCENARIO <path> <requests> <near misses> is each scenario.json of the
skill's folder under TRIGGER_SCENARIOS, the source tree's evaluation scenarios, whose mode is trigger, which judges
whether the description starts the skill, TRIGGER_SCENARIO_UNREADABLE <path> <reason> each scenario.json there that
cannot be read or is not a JSON object, and NO_TRIGGER_SCENARIO <path> names that folder when it holds neither.

Tool references: a tool counts as used on a line when its exact, case-sensitive name is
  - the start of an inline code span (`Read`, `Grep -l`, `Glob("*")`), outside fenced blocks;
  - followed by "(" (call syntax) or by the word tool(s), call(s), or invocation(s); or
  - a multi-word tool name that is not an English word (AskUserQuestion, WebFetch, mcp__*), anywhere.
A fence labelled for a command shell (bash, sh, shell, console, zsh, ksh, dash, fish, cmd, bat, or batch) or for
PowerShell (powershell, pwsh, or ps1) uses each of Bash and PowerShell that is allowed, else Bash: on Windows, Claude
Code may run a fence's command through either tool. Fences are read as ejryan89-skill-core's find_fences reads them, so
a fence counts wherever Markdown allows one: indented, in a list item, or in a blockquote, with backticks or tildes.
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

Provenance: provenance prints, for each check it is given, a line per source references/provenance.json names for
it: PAGE <check> <url> <passage> for a published page and the passage the check rests on, or DECISION <check> <sentence>
for a decision of this source. A check is a line the audit judges, CHECK_LINES, or a cue; any other name prints
NOT_A_CHECK <name>, and the command then exits 1. A provenance file without an entry for every check, or with one for
no check, is a failure.

Runtime prompts: RUNTIME_PROMPT <file> <line> <writer> is a subagent told to read a prompt file that the named script
(or unknown) writes at runtime, which the skill's own files never show. A skill source tree keeps a rendered sample of
each such prompt as docs/runtime-prompts/<skill>/<name>.txt, naming the throwaway folder it was rendered in as <temp>
and the source tree as <source>. For each sample of the skill, scan prints RUNTIME_PROMPT_SAMPLE <sample> <tokens>,
one RUNTIME_PROMPT_READ <sample> <path> <tokens> per file of the source tree the prompt names other than a script,
which the subagent reads on every run, and REPLY_BOUNDED <sample> yes|no, whether the prompt bounds the reply, then a
CUE line for each candidate in the sample's order and wording: after-reply at the first text after a reply line that
comes after the output contract starts (an "Output contract" line, or "Write one ... object", file, or line),
untrusted-after-contract at each line after the contract starts that marks data untrusted, and emphasis at each line
holding an emphatic word in capitals, such as MUST or NOT, but not a status value such as MUST_FIX. A skill with no
sample prints RUNTIME_PROMPT_UNSAMPLED <file> <line> <reason> for each runtime prompt instead.

Deterministic prose: besides the cost cues, scan names prose a script should replace, each a CUE line:
assembled-arguments (a flag chosen per printed line or value), parsed-output (output split, grouped, or renamed by a
fixed rule, or a printed value taken apart), carried-state (a count, summary, or last-seen value carried between
steps), owed-by-rules (the regenerations, gates, or follow-ups a change owes, decided by listed rules), and
hand-drafted (a file or message written by hand from a template or from printed lines).

Status lines: for a skill folder, scan reads the skill's own scripts, every script its Markdown's commands run,
through ${CLAUDE_SKILL_DIR} or from the repository root (the nearest folder above with a .git entry), the scripts
beside each of those, and ejryan89-skill-core's; it skips regression suites. A status word is an upper-case name of
three or more characters, such as SKILL_FILE. A script prints every status word its strings hold, outside docstrings,
and the FAILED line ejryan89-skill-core's run_main adds; it prints a line starting with a word when the word leads an
f-string, a string a print, write, append, insert, extend, emit, or echo call takes, a listed string, or a string of a
word, a space, and more. It exits 0, a literal an int-returning function returns or an exit call takes, 1 and 130 with
run_main, and 2 with argparse. UNPRINTED <file> <line> <word> is a word that leads a code span on a prose line about
output (one that says print, line, output, report, ends with, or exit) that no script prints, unless "such as" brings
it in as an example. EXIT_MISMATCH <file> <line> <code> is an exit code the prose states that no script uses.
UNEXPLAINED <script> <line> <word> is a line a script the Markdown runs starts with a word the Markdown never names,
for a script in the skill's folder or outside every skills folder, such as a repository tool; a sibling skill's script
is that skill's to explain. A skill with no script of its own that runs none prints none of these.
SCRIPT_MISSING <file> <line> <script> is a command that runs, through ${CLAUDE_SKILL_DIR} or from the repository root,
a script that does not exist, so nothing it would print is compared.

Runtime needs: in a skill source tree, tools derives what the skill needs through ejryan89-skill-core's
runtime_support, as repository validation does, and prints RUNTIME_NEEDS <capability> <evidence> for each: grant
<tool>, user-only, agent <name>, tool <name>, git <subcommand> <script>, or skill <name> for a skill it invokes that
needs it, without that skill's user-only start.
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import sys
from dataclasses import dataclass, field
from itertools import dropwhile, pairwise
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

import check_provenance
import frontmatter
import git_client
import json_reader
import runtime_support
import skill_menu
import whole_number
from console import run_main
from file_presence import is_present
from flat_text import flat_text, single_line
from git_client import GitClient
from markdown_sections import CODE_SPAN, POWERSHELL_FENCES, SHELL_FENCES, find_fences, prose_code_spans
from suite_names import is_suite_name

MAIN_NAME = "skill.md"
# Where Claude Code keeps the plugins it installs, under the home: each under its marketplace and version, so a plugin
# root lies PLUGIN_DEPTH folders down, as deep as locate searches any plugin folder.
PLUGIN_CACHE = Path(".claude") / "plugins" / "cache"
PLUGIN_DEPTH = 3
PLUGIN_SCOPE = "plugin"
SKILLS_FOLDER = "skills"
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
USAGE = "usage: audit-skill [<plugin>:]<SkillName> [<plugin folder>]"
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
# The end of a description's first sentence: a full stop, question, or exclamation mark before the next sentence's
# capital or the end, so an abbreviation such as "e.g." inside a sentence does not end it.
SENTENCE_END = re.compile(r"[.!?](?=\s+[A-Z]|\s*$)")
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
# Deterministic work a script should print or take, which the prose leaves to the agent. A code span names a printed
# value when it is not a flag; a status span starts with an upper-case name; an instruction opens its line, item, or
# sentence, or follows "then" or "and".
VALUE_SPAN = r"`(?!--)[^`\n]+`"
STATUS_SPAN = r"`[A-Z][A-Z0-9_]+[^`\n]*`"
FLAG_SPAN = r"`--[\w-]+[^`\n]*`"
INSTRUCTION = r"(?:^\s*(?:[-*+]|\d+\.)?\s*|[.:;]\s+|,\s+(?=print\b)|\b(?:then|and)\s+)"
PROSE_CUES = (
    # A flag chosen per printed line or value: "`--pull <pr>` for each `missing`", "one `--run` per `RUN` line",
    # "for a `SYNTHESIZED` line, `--synthesized`", "pass ... every `doc` path".
    (
        "assembled-arguments",
        re.compile(
            rf"(?i){FLAG_SPAN}[^.;]{{0,40}}?\b(?:for each|for every|per|when|for an?)\s+(?:\w+\s+){{0,3}}?{VALUE_SPAN}|"
            rf"\b(?:for each|for every|per|for an?)\s+{VALUE_SPAN}[^.;]{{0,60}}?{FLAG_SPAN}|"
            rf"\bpass(?:ing)?\b[^.;]{{0,60}}\b(?:every|each)\s+(?:\w+\s+)?{VALUE_SPAN}"
        ),
    ),
    # Output split, grouped, or sorted by a fixed rule: a file list split between readers, items prepared in groups,
    # a list item keyed on a message's text, or a printed line renamed to another status.
    (
        "parsed-output",
        re.compile(
            r"(?i)\bsplit(?:s|ting)?\s+(?:its|the|their|each)\s+(?:[\w-]+\s+)?(?:list|lines?|output|label)\s+"
            r"(?:by|between|into)\b|\bin (?:groups|batches) of up to\b|"
            rf"(?-i:^\s*(?:[-*+]|\d+\.)\s+\*\*`[a-z][^`]*\s[^`]*`)|\breport (?:it|them|that|this) as {STATUS_SPAN}"
        ),
    ),
    # A count, a summary, or a last-seen value carried from one step to a later one.
    (
        "carried-state",
        re.compile(
            r"(?i)\b(?:the last|the latest|the most recent)\b[^.;]{0,40}\bfor each\b|\bhow many\b|"
            r"\bper\b[^.;]{0,40}\bprinted\b|\b(?:keep|kept|keeping) (?:a |the )?(?:count|tally|running total)\b"
        ),
    ),
    # The regenerations, gates, or follow-ups a change owes, decided by listing the rules.
    (
        "owed-by-rules",
        re.compile(
            r"(?i)\b(?:after|when|whenever)\s+(?:any |a |an |the )?(?:[\w'-]+ ){0,5}?changes?"
            r"(?: to\b|,| touch(?:es)?\b)[^.;]{0,160}?\b(?:run|rerun|regenerate|rewrite|update)\b|"
            r"\bthe change touched\b|\bgo(?:es)? into the\b[^.;]{0,30}\bmilestone\b"
        ),
    ),
    # A file or a message the agent writes from a template or from printed lines by hand.
    (
        "hand-drafted",
        re.compile(
            rf"(?i){INSTRUCTION}(?:write|draft|compose|fill|build)\b[^.;]{{0,80}}\bfrom the\b[^.;]{{0,40}}\btemplate\b|"
            rf"\b(?:copy|paste|cite|include|quote|put)\b[^.;]{{0,40}}{STATUS_SPAN}(?:,\s*{STATUS_SPAN})+,?\s*(?:and|or)\s*"
            rf"{STATUS_SPAN}\s+lines?|"
            rf"{INSTRUCTION}(?:print|stop with|finish with|end with|reply with)\s+`[^`]*<[^`>]+>[^`]*`"
        ),
    ),
)
# A printed value split into its parts by hand: a span holding a space, a colon, and then two spans that are its parts,
# as "the label `v2 F002`: review version `2` and finding `F002`".
DECOMPOSED_VALUE = re.compile(r"`([^`\n]*\s[^`\n]*)`:\s([^.]*)")
VALUE_PART = re.compile(r"`([^`\s<-][^`\s]*)`")
# Inside code (fences and script files) only these cues apply; the rest describe prose.
CODE_CUES = frozenset({"wide-glob", "generated-code"})
# Cues scan names by a test of its own rather than by a pattern of CUES or PROSE_CUES.
SHELL_FILE_CUE = "shell-file-command"
AGENT_CUE = "agent"
SUBAGENT_REPLY_CUE = "subagent-reply"
# Cues on the shape of a runtime prompt sample rather than on a skill's own lines.
AFTER_REPLY_CUE = "after-reply"
UNTRUSTED_AFTER_CONTRACT_CUE = "untrusted-after-contract"
EMPHASIS_CUE = "emphasis"
SAMPLE_CUES = (AFTER_REPLY_CUE, UNTRUSTED_AFTER_CONTRACT_CUE, EMPHASIS_CUE)
CUE_NAMES = frozenset(
    {
        *(name for name, _ in CUES),
        *(name for name, _ in PROSE_CUES),
        SHELL_FILE_CUE,
        AGENT_CUE,
        SUBAGENT_REPLY_CUE,
        *SAMPLE_CUES,
    }
)
# The lines the audit judges, which with the cues are the checks references/provenance.json names the guidance of;
# every other line a command prints is a measurement or a location, which rests on no guidance.
CHECK_LINES = frozenset(
    {
        "AGENT_HOOK",
        "AGENT_UNDECLARED",
        "AGENT_UNDEFINED",
        "BODY_OVER_500_LINES",
        "DESCRIPTION",
        "DOC_NO_TOC",
        "DUPLICATE_BLOCK",
        "EXIT_MISMATCH",
        "EXPANDS",
        "GRANTED_REPOSITORY_CODE",
        "HIDDEN_ALLOWED",
        "IDLE_ALLOWED",
        "INLINED_HELPER",
        "INTERNAL_LISTED",
        "INVOCATION",
        "MISSING_ALLOWED",
        "MODEL",
        "NESTED_REFERENCE",
        "NO_TRIGGER_SCENARIO",
        "NO_SCRIPT_ALLOWED",
        "OPEN_PATH_ALLOWED",
        "OUTSIDE_MISSING",
        "OUTSIDE_READ",
        "PINNED_SUBAGENT",
        "PLUGIN_HOOK",
        "REPLY_BOUNDED",
        "RUNTIME_PROMPT",
        "RUNTIME_PROMPT_UNSAMPLED",
        "RUNTIME_OVERCLAIMED",
        "SCRIPT_MISSING",
        "SIBLING_FOLDER_ALLOWED",
        "UNEXPLAINED",
        "UNGRANTED",
        "UNGUARDED_SUBAGENT",
        "UNPAIRED_ALLOWED",
        "UNPRINTED",
        "UNSCOPED_ALLOWED",
        "UNUSED_ALLOWED",
    }
)
CHECKS = CHECK_LINES | CUE_NAMES
# A status word: the upper-case name a printed line starts with, such as FAILED or SKILL_FILE, of at least three
# characters, so a word such as ID names no line.
STATUS_WORD = r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*"
STATUS_WORD_LENGTH = 3
ANY_STATUS_WORD = re.compile(rf"(?<![\w-])({STATUS_WORD})(?![\w-])")
LINE_WORD = re.compile(rf"(?:^|\n)({STATUS_WORD})(?= |=|$)")
WORD_PREFIX = re.compile(rf"({STATUS_WORD}_)$")
LOWER_VALUE = re.compile(r"[a-z][a-z0-9_]*")
SPAN_STATUS = re.compile(rf"({STATUS_WORD})(?=$|\s)")
# A prose line about output, whose code spans name lines a script prints, and the words that introduce an example of
# another program's line, such as a subagent's reply.
OUTPUT_WORDS = re.compile(
    r"(?i)(?<![\w-])(?:prints?|printed|printing|lines?|outputs?|reports?|ends? with|exits?)(?![\w-])"
)
EXAMPLE_LEAD = re.compile(r"(?i)\b(?:such as|e\.g\.)\s*$")
EXIT_STATED = re.compile(
    r"(?i)\bexit(?:s|ing)?\s+(?:with\s+)?(?:(?:code|status)\s+)?(\d+)\b|\bexit (?:code|status) (?:of )?(\d+)\b"
)
SHELL_PRINT = re.compile(
    rf"\b(?:echo|printf|Write-Output|Write-Host)\s+(?:-\w+\s+)*[\"']?({STATUS_WORD})(?=[\s\"'=]|$)"
)
SHELL_EXIT = re.compile(r"\bexit\s+(\d+)\b")
STATUS_SCRIPT_SUFFIXES = frozenset({".py", ".sh", ".bash", ".ps1"})
SKILL_DIR_PREFIX = "${CLAUDE_SKILL_DIR}/"
# The calls that print a line or add one to the lines a command prints, and those that exit with a code.
PRINTING_CALLS = frozenset({"print", "write", "append", "insert", "extend", "emit", "echo"})
EXIT_CALLS = frozenset({"exit", "_exit", "SystemExit"})
# What every entry point gets from ejryan89-skill-core's run_main, a FAILED line and exits 1 and 130, and from argparse,
# 2.
RUN_MAIN_CODES = frozenset({1, 130})
USAGE_CODE = 2
# Lines that bound what a subagent sends back; without one, every reply lands in the orchestrator's context.
REPLY_BOUND = re.compile(r"(?i)\breply (?:with|only)\b|\brespond only\b|\breturn only\b|\bfinal (?:message|reply)\b")
DELEGATION = re.compile(r"(?i)\bsubagents?\b")
# A subagent told to read a prompt file: the real prompt is written at runtime and the audit cannot see it.
RUNTIME_PROMPT = re.compile(r"(?i)\bread <[^>]*\bprompt\b[^>]*>")
SCRIPT_PATH = re.compile(r"[\w./{}$-]*scripts/[\w.-]+\.(?:py|sh|bash|ps1|js|mjs)")
# Where a runtime prompt's output contract starts, a block the prompt marks as untrusted data, and an emphatic word
# in capitals; a word of a status value such as MUST_FIX or NOT_FOUND is not emphasis.
OUTPUT_CONTRACT = re.compile(r"(?i)^\s*(?:output contract\b|write (?:exactly )?one\b.*\b(?:object|file|line)\b)")
UNTRUSTED_DATA = re.compile(r"(?i)\buntrusted\b")
EMPHASIS = re.compile(
    r"(?<![\w-])(?:MUST|NEVER|ALWAYS|NOT|CRITICAL|IMPORTANT|ONLY|AUTHORITATIVE|REQUIRED|EXACTLY|WARNING)(?![\w-])"
)
# Where a skill source tree keeps the rendered samples of its runtime prompts, one folder per skill.
RUNTIME_PROMPT_SAMPLES = Path("docs") / "runtime-prompts"
# Where a skill source tree keeps the scenarios that judge each skill, one folder per skill.
TRIGGER_SCENARIOS = Path("tests") / "fixtures" / "skill-evals"
# A file of the source tree a rendered sample names, which the renderer wrote as <source> and the path below it.
SAMPLE_SOURCE_PATH = re.compile(r"<source>[\\/]([^\s\"'`<>]+)")
# A relative Markdown path that leaves the skill folder, such as `../shared.md`. With a ${CLAUDE_SKILL_DIR}
# prefix it resolves against the skill folder instead of the file that names it.
# Where agent definitions sit beside a skills folder, and the subagent types a session has without one: the sub-agents
# page (https://code.claude.com/docs/en/sub-agents) names Explore, Plan, and the general-purpose agent, which has every
# tool. A type a delegation line names: "of type `x`", "`x` type", "`x` subagent", or subagent_type x.
AGENTS_FOLDER = "agents"
BUILTIN_AGENTS = frozenset({"general-purpose", "Explore", "Plan"})
# A plugin's agent is named under the plugin, as <plugin>:<agent>.
AGENT_NAME = r"[A-Za-z][A-Za-z0-9-]*(?::[A-Za-z][A-Za-z0-9-]*)?"
TYPED_AGENT = re.compile(
    rf"\btype\s+`({AGENT_NAME})`|`({AGENT_NAME})`\s+(?:type|subagent|agent)s?\b|\bsubagent_type\b\W{{0,4}}({AGENT_NAME})"
)
GENERAL_PURPOSE = re.compile(r"(?i)(?<![\w-])general-purpose(?![\w-])")
# A key of a hooks value's line, after any sequence item's dash, and a block scalar's header.
HOOK_KEY = re.compile(r"^(-\s+)?([A-Za-z][\w-]*)\s*:(?:\s+(.*))?$")
BLOCK_SCALAR = re.compile(r"[|>][+-]?")
# A subagent's model or effort. A sentence that starts a subagent names a literal one as "on haiku" or "at low
# effort", or relays a printed line's value as a code span whose status word holds MODEL or EFFORT. A data file sets
# one under a key SETTING_KEYS names, to a model alias or ID or an effort level.
START_VERB = re.compile(r"(?i)\b(?:start|starts|starting|spawn|spawns|launch|launches|run|runs)\b")
LITERAL_MODEL = re.compile(r"(?i)\bon\s+(?:the\s+)?`?(haiku|sonnet|opus|fable)`?(?![\w-])")
LITERAL_EFFORT = re.compile(
    r"(?i)\bat\s+(?:an?\s+)?`?(minimal|low|medium|high|xhigh|max)`?\s+(?:reasoning\s+)?effort\b"
)
STATUS_SPAN_WORD = re.compile(r"`([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*)(?:\s[^`]*)?`")
SETTING_WORDS = ("model", "effort")
SETTING_KEYS = {"model": "model", "effort": "effort", "reasoning_effort": "effort", "reviewer_effort": "effort"}
MODEL_VALUE = re.compile(r"(?i)haiku|sonnet|opus|fable|claude-[\w.-]+")
EFFORT_LEVELS = frozenset({"minimal", "low", "medium", "high", "xhigh", "max"})
# A sibling skill a skill's Markdown reaches through its own directory.
SIBLING_SKILL = re.compile(r"\$\{CLAUDE_SKILL_DIR\}/\.\./([A-Za-z0-9._-]+)/")
# A git command where a Bash command starts: at the line's start, after an operator, a substitution, or a keyword.
GIT_COMMAND = re.compile(
    r"(?:^|[;&|(`]|\$\(|\b(?:then|do|else|if|elif|while|until|time|command|exec)\s|!\s)\s*git\s+([^;&|)`\n]*)"
)
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
    """A file's UTF-8 text; one that cannot be read, such as a directory in its place, or is binary or not UTF-8 fails
    rather than reading as empty."""
    try:
        text = read_text(path)
    except OSError as exc:
        raise InputError(f"cannot read {posix(path)}: {exc.strerror or exc}") from exc
    if text is None:
        raise InputError(f"cannot read {posix(path)}: {NOT_UTF8}")
    return text


@dataclass
class Block:
    line: int
    language: str
    body: list[str] = field(default_factory=list)


def parse_fences(lines: list[str]) -> tuple[list[str | None], list[Block]]:
    """Return each line's fence language (None outside fences) and every fenced block, as ejryan89-skill-core's fence
    reader finds them, each body line without the blockquote markers that hold it."""
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
    # ValueError covers, before 3.12, a null byte; MemoryError and RecursionError, source nested past the parser's
    # limits
    except (SyntaxError, ValueError, MemoryError, RecursionError):
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
                        f"GRANTED_REPOSITORY_CODE {tool} {number} {command}"
                        if covered
                        else f"REPOSITORY_CODE {tool} {number} {command}"
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


def plugin_roots(folder: Path, depth: int = PLUGIN_DEPTH) -> list[Path]:
    """Each plugin root at or below folder, down to depth folders, never searching inside a plugin."""
    if (folder / skill_menu.PLUGIN_MANIFEST).is_file():
        return [folder]
    if depth == 0 or not folder.is_dir():
        return []
    children = sorted((child for child in folder.iterdir() if child.is_dir()), key=lambda path: path.name.casefold())
    return [root for child in children for root in plugin_roots(child, depth - 1)]


def manifest_name(root: Path) -> str | None:
    """A plugin's name, from its manifest, or None when the manifest names none."""
    try:
        document = json_reader.read_file(root / skill_menu.PLUGIN_MANIFEST)
    # ValueError covers a decode error, UnicodeError, and an over-long integer; RecursionError, nesting too deep
    except (OSError, ValueError, RecursionError):
        return None
    name = document.get("name") if isinstance(document, dict) else None
    return name if isinstance(name, str) and SKILL_NAME.fullmatch(name) else None


def plugin_of(directory: Path) -> tuple[str, Path] | None:
    """The name and root of the plugin a skill folder belongs to, or None outside a plugin."""
    root = directory.parent.parent
    name = manifest_name(root) if directory.parent.name == SKILLS_FOLDER else None
    return (name, root) if name is not None else None


def plugin_skill_roots(folder: Path, plugin: str) -> list[tuple[str, Path]]:
    """The skills folder of each plugin found under folder, of the named plugin alone when one is named."""
    return [
        (PLUGIN_SCOPE, root / SKILLS_FOLDER)
        for root in plugin_roots(folder)
        if (found := manifest_name(root)) is not None and plugin in ("", found)
    ]


def search_roots(home: Path, repo: Path | None, plugin: str, folder: Path | None) -> list[tuple[str, Path]]:
    """Where locate looks: the plugins under a folder the user names, or the skill roots and then Claude Code's plugin
    cache, which alone is searched for a skill named with its plugin."""
    if folder is not None:
        if not folder.is_dir():
            raise InputError(f"plugin folder not found: {posix(folder)}")
        return plugin_skill_roots(folder, plugin)
    cache = plugin_skill_roots(home / PLUGIN_CACHE, plugin)
    return cache if plugin else [*skill_roots(home, repo), *cache]


def preferred(hits: list[tuple[str, Path]], name: str) -> list[tuple[str, Path]]:
    """The copies of a skill the audit means, of those found."""
    scopes = {scope for scope, _ in hits}
    # A developer in the source tree means the source, not the deployed copy of it.
    if "source" in scopes:
        hits = [(scope, path) for scope, path in hits if scope != "user"]
    # A .agents/skills shim that sends other runtimes to the .claude/skills skill of its name is that skill.
    if "project-claude" in scopes:
        pointer = f".claude/skills/{name}/SKILL.md"
        hits = [
            (scope, path)
            for scope, path in hits
            if not (scope == "project-agents" and pointer in (read_text(path) or ""))
        ]
    # An installed plugin's copy is audited when it is the only one, or when the plugin is named.
    if scopes - {PLUGIN_SCOPE}:
        hits = [(scope, path) for scope, path in hits if scope != PLUGIN_SCOPE]
    return hits


def shown_name(scope: str, directory: Path) -> str:
    """A skill's name as its runtime lists it: a plugin's skill under the plugin's name."""
    plugin = plugin_of(directory) if scope == PLUGIN_SCOPE else None
    return f"{plugin[0]}:{directory.name}" if plugin is not None else directory.name


def found_lines(scope: str, path: Path) -> list[str]:
    lines = [f"SKILL_FILE {posix(path)}", f"SKILL_DIR {posix(path.parent)}", f"SCOPE {scope}"]
    plugin = plugin_of(path.parent) if scope == PLUGIN_SCOPE else None
    return [*lines, f"PLUGIN {plugin[0]} {posix(plugin[1])}"] if plugin is not None else lines


def available_lines(name: str, roots: list[tuple[str, Path]]) -> list[str]:
    lines = [f"NOT_FOUND {name}"]
    listed: set[str] = set()
    available: dict[str, list[str]] = {}
    for scope, root in roots:
        if not root.is_dir():
            continue
        for directory in sorted(root.iterdir(), key=lambda path: path.name.casefold()):
            if directory.is_dir() and skill_files(scope, directory) and identity(directory) not in listed:
                listed.add(identity(directory))
                lines.append(f"AVAILABLE {scope} {shown_name(scope, directory)}")
                available.setdefault(scope, []).append(shown_name(scope, directory))
    groups = " / ".join(f"{scope}: {', '.join(names)}" for scope, names in available.items()) or "none"
    return [*lines, f"STOP skill '{name}' not found. Available skills: {groups}"]


def locate(name: str, home: Path, repo: Path | None, folder: Path | None = None) -> list[str]:
    """Find a skill by its name, or by <plugin>:<name>, and in the plugins under folder when one is given."""
    if not name:
        return [f"STOP {USAGE}"]
    plugin, _, skill = name.rpartition(":")
    if not SKILL_NAME.fullmatch(skill) or (plugin and not SKILL_NAME.fullmatch(plugin)):
        raise InputError(f"invalid skill name {name!r}")
    roots = search_roots(home, repo, plugin, folder)
    hits: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for scope, root in roots:
        for path in skill_files(scope, root / skill):
            if identity(path) not in seen:
                seen.add(identity(path))
                hits.append((scope, path))
    hits = preferred(hits, skill)
    lines = [f"REPO {posix(repo)}" if repo is not None else "REPO none"]
    if len(hits) == 1:
        return [*lines, *found_lines(*hits[0])]
    if hits:
        paths = ", ".join(posix(path) for _, path in hits)
        plugins = all(scope == PLUGIN_SCOPE for scope, _ in hits)
        advice = (
            "Name the plugin, as <plugin>:<skill>, or its folder." if plugins else "Rename one or delete the duplicate."
        )
        return [
            *lines,
            *(f"AMBIGUOUS {scope} {posix(path)}" for scope, path in hits),
            f"STOP ambiguous skill name '{name}' — found at: {paths}. {advice}",
        ]
    return [*lines, *available_lines(name, roots)]


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


def folder_files(directory: Path) -> list[Path]:
    """Every file of a skill folder that inventory sizes, in the order it lists them."""
    return sorted(
        (
            path
            for path in directory.rglob("*")
            if path.is_file() and not SKIPPED_DIRECTORIES.intersection(path.relative_to(directory).parts[:-1])
        ),
        key=lambda path: posix(path.relative_to(directory)).casefold(),
    )


def inventory(directory: Path) -> list[str]:
    if not directory.is_dir():
        raise InputError(f"skill directory not found: {directory}")
    files = folder_files(directory)
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
    lines.extend(f"TOTAL {kind} {totals[kind][0]} {totals[kind][1]} {totals[kind][2]}" for kind in CATEGORIES)
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
        *plugin_lines(directory),
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


def read_metadata(root: Path, name: str) -> dict[str, object] | None:
    """A skill's deploy metadata in its source tree, or None when the file is missing or not a JSON object."""
    try:
        metadata = json_reader.read_file(root / "deploy-meta" / f"{name}.json")
    # ValueError covers a decode error, UnicodeError, and an over-long integer; RecursionError, nesting too deep
    except (OSError, ValueError, RecursionError):
        return None
    return metadata if isinstance(metadata, dict) else None


def names(metadata: dict[str, object] | None, key: str) -> list[str]:
    """The names a metadata list holds, or none for a key that is absent or not a list of names."""
    value = (metadata or {}).get(key)
    return [name for name in value if isinstance(name, str)] if isinstance(value, list) else []


def skill_closure(root: Path, name: str) -> list[str]:
    """The skill and every skill its metadata's skill_deps reach, in the order they are first reached."""
    reached: list[str] = []
    queue = [name]
    while queue:
        current = queue.pop(0)
        if current not in reached:
            reached.append(current)
            queue += names(read_metadata(root, current), "skill_deps")
    return reached


def declared_agents(directory: Path) -> dict[str, str] | None:
    """The agents a deployment of the skill installs, each with the skill that declares it in agent_deps, its own
    first: None outside a source tree, where no metadata says."""
    root = source_root(directory)
    if root is None:
        return None
    agents: dict[str, str] = {}
    for skill in skill_closure(root, directory.name):
        for agent in names(read_metadata(root, skill), "agent_deps"):
            agents.setdefault(agent, skill)
    return agents


def declared_lines(directory: Path) -> list[str]:
    """The skill's declared shared assets, skill dependencies, agents, and tools, when it is audited from its source
    tree: an agent a skill dependency declares is installed with the skill too, so it is named with that skill."""
    root = source_root(directory)
    if root is None:
        return []
    declared = read_metadata(root, directory.name)
    if declared is None:
        return [f"DECLARED_UNREADABLE {posix(root / 'deploy-meta' / f'{directory.name}.json')}"]
    agents = declared_agents(directory) or {}
    return [
        *(f"DECLARED shared {name}" for name in names(declared, "shared_deps")),
        *(f"DECLARED skill {name}" for name in names(declared, "skill_deps")),
        *(
            f"DECLARED agent {agent}" if skill == directory.name else f"DECLARED agent {agent} via {skill}"
            for agent, skill in agents.items()
        ),
        *(f"DECLARED tool {name}" for name in names(declared, "tools")),
        *(f"DECLARED optional-tool {name}" for name in names(declared, "optional_tools")),
    ]


def plugin_lines(directory: Path) -> list[str]:
    """Which plugins carry the skill: for a plugin's copy, its plugin and what that plugin carries beside it; in a
    source tree with a marketplace file, each plugin the source makes that carries it, or why none does."""
    plugin = plugin_of(directory)
    if plugin is not None:
        name, plugin_root = plugin
        skills = [
            child.name
            for child in sorted((plugin_root / SKILLS_FOLDER).iterdir(), key=lambda path: path.name.casefold())
            if child.is_dir() and child.name != directory.name and main_files(child)
        ]
        return [
            f"CARRIED_BY {name}",
            *(f"PLUGIN_CARRIES skill {skill}" for skill in skills),
            *(f"PLUGIN_CARRIES agent {agent}" for agent in defined_agents(directory)),
        ]
    root = source_root(directory)
    if root is None or not (root / skill_menu.PLUGIN_MARKETPLACE).is_file():
        return []
    try:
        carried, reasons = skill_menu.carriers(skill_menu.read_menu(root), directory.name)
    except skill_menu.MenuError as exc:
        return [f"PLUGINS_UNREADABLE {exc}"]
    return [*(f"CARRIED_BY {name}" for name in carried), *(f"LEFT_OUT {item} {why}" for item, why in reasons.items())]


def trigger_scenario_lines(directory: Path, invocation: str) -> list[str]:
    """The trigger scenario a model-invocable skill's description is judged by, when the skill is audited from its
    source tree, TRIGGER_SCENARIO_UNREADABLE for a scenario file it cannot read, or NO_TRIGGER_SCENARIO when it has
    neither."""
    root = source_root(directory)
    if root is None or invocation != "model":
        return []
    scenarios = root / TRIGGER_SCENARIOS / directory.name
    output = []
    for spec_file in sorted(scenarios.glob("*/scenario.json")):
        try:
            spec = json_reader.read_file(spec_file)
        except OSError as exc:
            output.append(f"TRIGGER_SCENARIO_UNREADABLE {posix(spec_file)} {exc.strerror or exc}")
            continue
        # ValueError covers a decode error, UnicodeError, and an over-long integer; RecursionError, nesting too deep
        except (ValueError, RecursionError) as exc:
            output.append(f"TRIGGER_SCENARIO_UNREADABLE {posix(spec_file)} not JSON: {exc}")
            continue
        if not isinstance(spec, dict):
            output.append(f"TRIGGER_SCENARIO_UNREADABLE {posix(spec_file)} not a JSON object")
        elif spec.get("mode") == "trigger":
            requests, near_misses = (spec.get(key) for key in ("requests", "near_misses"))
            counts = [len(value) if isinstance(value, list) else 0 for value in (requests, near_misses)]
            output.append(f"TRIGGER_SCENARIO {posix(spec_file)} {counts[0]} {counts[1]}")
    return output or [f"NO_TRIGGER_SCENARIO {posix(scenarios)}"]


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
    invocation = next(line.split()[1] for line in listed if line.startswith("INVOCATION "))
    output = [f"MODEL {model or 'none'}"]
    if path.name.casefold() == MAIN_NAME:
        output += setting_lines(path.parent)
    output += [*listed, *trigger_scenario_lines(path.parent, invocation)]
    output += runtime_lines(path.parent, allowed or [], invocation != "model")
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

    model_invocable = not document.user_only()
    user_invocable = flag("user-invocable") != "false"
    invocation = "model" if model_invocable else ("user-only" if user_invocable else "hidden")
    lines = [f"DESCRIPTION {len(text)} {-(-len(text) // 4)}", f"INVOCATION {invocation}"]
    if model_invocable and text:
        lines.append(f"DESCRIPTION_LEAD {description_lead(text)}")
    if model_invocable and INTERNAL_WORDING.search(text):
        lines.append("INTERNAL_LISTED")
    return lines


def description_lead(text: str) -> str:
    """The description's first sentence on one line: what a runtime that shortens a long listing keeps longest."""
    end = SENTENCE_END.search(text)
    return flat_text(text[: end.end()] if end else text)


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


def decomposes_a_value(line: str) -> bool:
    """Whether the line shows a printed value with a space and then names two of its parts, by DECOMPOSED_VALUE."""
    for match in DECOMPOSED_VALUE.finditer(line):
        if sum(part in match.group(1) for part in VALUE_PART.findall(match.group(2))) >= 2:
            return True
    return False


def scanned_files(path: Path) -> list[Path]:
    """A file as given, or a skill folder's main file followed by every doc file inventory lists."""
    if not path.is_dir():
        return [path]
    files = folder_files(path)
    main = [file for file in files if category(file.relative_to(path)) == "main"]
    if not main:
        raise InputError(f"no SKILL.md in {posix(path)}")
    return [*main, *(file for file in files if category(file.relative_to(path)) == "doc")]


def scan(given: list[Path]) -> list[str]:
    paths = [file for path in given for file in scanned_files(path)]
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
            cues += [
                name
                for name, pattern in PROSE_CUES
                if pattern.search(line) or (name == "parsed-output" and decomposes_a_value(line))
            ]
            if language is not None:
                cues = [cue for cue in cues if cue in CODE_CUES]
            shell = (
                FENCE_SHELL_COMMAND.search(line)
                if language is not None
                else any(SPAN_SHELL_COMMAND.match(span.strip()) for span in CODE_SPAN.findall(line))
            )
            if shell:
                cues.append(SHELL_FILE_CUE)
            if language is None and (DELEGATION.search(line) or line_references(line, None, ["Agent", "Task"], [])):
                cues.append(AGENT_CUE)
            if "wide-glob" in cues and "head_limit" in line:
                cues.remove("wide-glob")
            excerpt = text if len(text) <= 160 else text[:157] + "..."
            output.extend(f"CUE {posix(path)} {index + 1} {cue} {excerpt}" for cue in cues)
        if path.suffix.casefold() == ".md":
            found = delegation_lines(path, lines, languages, start)
            output.extend(found)
            runtime_prompts += [(path, int(line.split()[2])) for line in found if line.startswith("RUNTIME_PROMPT ")]
    return (
        output
        + [line for path in given if path.is_dir() for line in agent_lines(path)]
        + sample_lines(runtime_prompts)
        + [line for path in given if path.is_dir() for line in status_lines(path)]
    )


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
        output.append(f"CUE {posix(path)} {delegations[0] + 1} {SUBAGENT_REPLY_CUE} {excerpt}")
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
    return output + sample_shape_cues(sample, text.splitlines())


def sample_shape_cues(sample: Path, lines: list[str]) -> list[str]:
    """Candidates in a rendered prompt's order and wording: the first text after a reply line that ends the output
    contract, each line marking untrusted data after the contract starts, and each line with capitalised emphasis."""
    contract = next((index for index, line in enumerate(lines) if OUTPUT_CONTRACT.search(line)), None)
    replies = [index for index, line in enumerate(lines) if REPLY_BOUND.search(line)]
    found: list[tuple[int, str]] = []
    if contract is not None and replies and replies[-1] > contract:
        after = next((index for index in range(replies[-1] + 1, len(lines)) if lines[index].strip()), None)
        if after is not None:
            found.append((after, AFTER_REPLY_CUE))
    if contract is not None:
        found += [
            (index, UNTRUSTED_AFTER_CONTRACT_CUE)
            for index in range(contract + 1, len(lines))
            if UNTRUSTED_DATA.search(lines[index])
        ]
    found += [(index, EMPHASIS_CUE) for index, line in enumerate(lines) if EMPHASIS.search(line)]
    output = []
    for index, cue in sorted(found):
        text = lines[index].strip()
        excerpt = text if len(text) <= 160 else text[:157] + "..."
        output.append(f"CUE {posix(sample)} {index + 1} {cue} {excerpt}")
    return output


# --- status lines ---------------------------------------------------------------------------------


@dataclass
class ScriptFacts:
    """What a script's text shows it prints and exits with."""

    words: set[str] = field(default_factory=set)  # every status word its strings hold, anywhere in them
    prefixes: set[str] = field(default_factory=set)  # a word's start a formatted value completes, such as CONFIG_
    lines: dict[str, int] = field(default_factory=dict)  # the word each line it prints starts with, and where
    codes: set[int] = field(default_factory=lambda: {0})


def docstring_ids(tree: ast.Module) -> set[int]:
    """The string constants that are a module's, a class's, or a function's docstring, which print nothing."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                found.add(id(first.value))
    return found


def call_name(node: ast.AST | None) -> str:
    if not isinstance(node, ast.Call):
        return ""
    return node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")


def printed_text(node: ast.Constant, parents: dict[int, ast.AST]) -> str | None:
    """The text a string constant gives a line the script prints, or None for a value it compares, looks up, or
    passes to anything but a print: an f-string's leading text, a printed or listed string, or one that starts with
    a status word and a space, outside a comparison, a subscript, a keyword argument, a dict, or a call."""
    if not isinstance(node.value, str):
        return None
    parent = parents.get(id(node))
    if isinstance(parent, ast.JoinedStr):
        leading = parent.values[0] is node and re.match(rf"{STATUS_WORD}(?: |$)", node.value)
        return node.value if leading else None
    if call_name(parent) in PRINTING_CALLS:
        return node.value
    if isinstance(parent, ast.List):
        holder = parents.get(id(parent))
        lookup = isinstance(holder, ast.Call | ast.Compare | ast.Subscript | ast.keyword | ast.Dict)
        return None if lookup and call_name(holder) not in PRINTING_CALLS else node.value
    if isinstance(parent, ast.Compare | ast.Subscript | ast.keyword | ast.Dict | ast.Call):
        return None
    return node.value if re.match(rf"{STATUS_WORD} \S", node.value) else None


def string_facts(facts: ScriptFacts, node: ast.Constant, value: str, parents: dict[int, ast.AST]) -> None:
    facts.words.update(ANY_STATUS_WORD.findall(value))
    if LOWER_VALUE.fullmatch(value):  # a state such as "overdue" the script may print upper-cased
        facts.words.add(value.upper())
    facts.prefixes.update(WORD_PREFIX.findall(value))
    text = printed_text(node, parents)
    for match in LINE_WORD.finditer(text or ""):
        if len(match.group(1)) >= STATUS_WORD_LENGTH:
            facts.lines.setdefault(match.group(1), node.lineno)


def exit_codes(tree: ast.Module) -> set[int]:
    """The literal codes a script exits with: what a function annotated to return an int returns, and what an exit
    call or SystemExit takes, each a number or a module-level name bound to one."""
    constants = {
        target.id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and type(node.value.value) is int
        for target in node.targets
        if isinstance(target, ast.Name)
    }

    def literal(node: ast.AST) -> list[int]:
        if isinstance(node, ast.IfExp):
            return literal(node.body) + literal(node.orelse)
        if isinstance(node, ast.Constant) and type(node.value) is int:
            return [node.value]
        return [constants[node.id]] if isinstance(node, ast.Name) and node.id in constants else []

    codes: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and isinstance(node.returns, ast.Name) and node.returns.id == "int":
            for inner in ast.walk(node):
                if isinstance(inner, ast.Return) and inner.value is not None:
                    codes.update(literal(inner.value))
        elif call_name(node) in EXIT_CALLS and isinstance(node, ast.Call) and node.args:
            codes.update(literal(node.args[0]))
    return codes


def python_facts(text: str) -> ScriptFacts:
    tree = ast.parse(text)
    facts = ScriptFacts()
    docstrings = docstring_ids(tree)
    parents = {id(child): node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            string_facts(facts, node, node.value, parents)
        elif isinstance(node, ast.Constant) and isinstance(node.value, bytes):
            string_facts(facts, node, node.value.decode("latin-1"), parents)
    facts.codes |= exit_codes(tree)
    names = {
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import | ast.ImportFrom) for alias in node.names
    }
    if "run_main" in names:
        facts.words.add("FAILED")
        facts.codes |= RUN_MAIN_CODES
    if "argparse" in names:
        facts.codes.add(USAGE_CODE)
    return facts


def shell_facts(text: str) -> ScriptFacts:
    facts = ScriptFacts()
    for number, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue
        facts.words.update(ANY_STATUS_WORD.findall(line))
        for match in SHELL_PRINT.finditer(line):
            facts.lines.setdefault(match.group(1), number)
        # A code past the digit limit, or in digits other than ASCII, is no code a shell exits with.
        codes = (whole_number.parse(code) for code in SHELL_EXIT.findall(line))
        facts.codes.update(code for code in codes if code is not None)
    return facts


def script_facts(path: Path) -> ScriptFacts:
    """What a script prints and exits with. One that is not UTF-8 text or not valid Python fails: what it prints
    cannot be measured, and reading it as printing nothing would compare the Markdown with less than the skill runs."""
    text = require_text(path)
    if path.suffix.casefold() != ".py":
        return shell_facts(text)
    try:
        return python_facts(text)
    # ValueError covers, before 3.12, a null byte; MemoryError and RecursionError, source nested past the parser's
    # limits
    except (SyntaxError, ValueError, MemoryError, RecursionError) as exc:
        raise InputError(f"cannot read {posix(path)}: not valid Python") from exc


def repository_root(folder: Path) -> Path | None:
    """The nearest folder above a skill that holds a .git entry, where its commands run tools from, or None."""
    return next((parent for parent in folder.resolve().parents if (parent / ".git").exists()), None)


def folder_scripts(folder: Path) -> list[Path]:
    """The scripts of a folder, resolved, without its regression suites, which are no skill's output."""
    if not folder.is_dir():
        return []
    return [
        path.resolve()
        for path in sorted(folder.iterdir())
        if path.is_file() and path.suffix.casefold() in STATUS_SCRIPT_SUFFIXES and not is_suite_name(path.name)
    ]


def command_scripts(
    folder: Path, root: Path | None, markdown: list[tuple[Path, list[str]]]
) -> tuple[list[Path], list[str]]:
    """The scripts the Markdown's commands run, through the skill-directory variable or from the repository root, as a
    repository skill runs a tool: those that exist, and a SCRIPT_MISSING line for each command naming one that does
    not. A directory in a script's place raises file_presence's NotAFile."""
    found: list[Path] = []
    missing: list[str] = []
    for path, lines in markdown:
        start = body_start(lines)
        body = lines[start:]
        for line, command in fence_commands(body, start) + span_commands(body, start):
            for match in SCRIPT_FILE.finditer(command):
                text = match.group(0).strip("\"'")
                # A suffix that runs on, as .js in "<report.json>", names no script.
                if re.match(r"[\w.-]", command[match.end() :]):
                    continue
                if text.startswith(SKILL_DIR_PREFIX):
                    script = folder / text.removeprefix(SKILL_DIR_PREFIX)
                elif root is not None and not text.startswith(("$", "~", "/")):
                    script = root / text
                else:
                    continue
                if is_present(script):
                    found.append(script.resolve())
                # A bare name from the repository root, such as a value a span gives, names no tool's path.
                elif text.startswith(SKILL_DIR_PREFIX) or "/" in text:
                    missing.append(f"SCRIPT_MISSING {posix(path)} {line} {text}")
    return list(dict.fromkeys(found)), list(dict.fromkeys(missing))


def shown_path(folder: Path, path: Path) -> str:
    """A script's path under the skill folder as it was given, or its resolved path outside it."""
    try:
        return posix(folder / path.relative_to(folder.resolve()))
    except ValueError:
        return posix(path)


def stated_lines(folder: Path, path: Path, lines: list[str], printed: set[str], prefixes: set[str]) -> list[str]:
    """Each status word a code span names on a prose line about output that no script prints, outside an example
    introduced by "such as", and outside the words a printed prefix such as CONFIG_ may complete."""
    output: list[str] = []
    languages, _ = parse_fences(lines)
    for index in range(body_start(lines), len(lines)):
        line = lines[index]
        if languages[index] is not None or not OUTPUT_WORDS.search(line):
            continue
        for span in CODE_SPAN.finditer(line):
            word = SPAN_STATUS.match(span.group(1).strip())
            if (
                word
                and len(word.group(1)) >= STATUS_WORD_LENGTH
                and not EXAMPLE_LEAD.search(line[: span.start()])
                and word.group(1) not in printed
                and not word.group(1).startswith(tuple(prefixes))
            ):
                output.append(f"UNPRINTED {posix(path)} {index + 1} {word.group(1)}")
    return output


def stated_code(digits: str) -> int | str:
    """The exit code a line states, or its digits as stated when they are too many to read, which no script exits
    with."""
    code = whole_number.parse(digits)
    return digits if code is None else code


def stated_codes(path: Path, lines: list[str], codes: set[int]) -> list[str]:
    languages, _ = parse_fences(lines)
    return [
        f"EXIT_MISMATCH {posix(path)} {index + 1} {code}"
        for index in range(body_start(lines), len(lines))
        if languages[index] is None
        for match in EXIT_STATED.finditer(lines[index])
        if (code := stated_code(match.group(1) or match.group(2))) not in codes
    ]


def status_lines(folder: Path) -> list[str]:
    """Compare the status lines and exit codes a skill's Markdown names with what its scripts print and exit with.

    The scripts read are the skill's own, every script its commands run and the scripts beside it, and
    ejryan89-skill-core's. UNPRINTED is a status word a code span names on a prose line about output that none of them
    prints; EXIT_MISMATCH an exit code the prose states that none of them uses; UNEXPLAINED a line that a script the
    skill runs, its own or a repository tool, starts with a status word the Markdown never names; SCRIPT_MISSING a
    command naming a script that does not exist. A skill with no script of its own that runs none has nothing else to
    compare.
    """
    markdown = [(path, require_text(path).splitlines()) for path in scanned_files(folder)]
    root = repository_root(folder)
    bodies = [lines[body_start(lines) :] for _, lines in markdown]
    commands, missing = command_scripts(folder, root, markdown)
    own = folder_scripts(folder / "scripts")
    if not own and not commands:
        return missing
    cores = [
        folder.parent / "ejryan89-skill-core" / "scripts",
        *([root / "skills" / "ejryan89-skill-core" / "scripts"] if root else []),
    ]
    reading = [*own, *commands, *(path for command in commands for path in folder_scripts(command.parent))]
    reading += [path for core in cores for path in folder_scripts(core)]
    skills_roots = {folder.resolve().parent, *((root / "skills", root / ".claude" / "skills") if root else ())}
    runs = [path for path in commands if folder.resolve() in path.parents or skills_roots.isdisjoint(path.parents)]
    facts = ScriptFacts()
    first_lines: dict[str, tuple[Path, int]] = {}
    for path in dict.fromkeys(reading):
        found = script_facts(path)
        facts.words |= found.words
        facts.prefixes |= found.prefixes
        facts.codes |= found.codes
        if path in runs:
            for word, number in found.lines.items():
                first_lines.setdefault(word, (path, number))
    output = [
        line for path, lines in markdown for line in stated_lines(folder, path, lines, facts.words, facts.prefixes)
    ]
    output += [line for path, lines in markdown for line in stated_codes(path, lines, facts.codes)]
    named = "\n".join("\n".join(body) for body in bodies)
    unnamed = [
        (shown_path(folder, path), number, word)
        for word, (path, number) in first_lines.items()
        if not re.search(rf"(?<![\w-]){word}(?![\w-])", named)
    ]
    return missing + output + [f"UNEXPLAINED {path} {number} {word}" for path, number, word in sorted(unnamed)]


# --- subagents ------------------------------------------------------------------------------------


@dataclass
class Hook:
    """One command a subagent's definition runs on an event: what it matches, the program it starts, and its limits."""

    event: str
    group: int
    program: str = "unknown"
    timeout: str = "default"
    on_failure: str = "default"


@dataclass
class AgentDefinition:
    """What an agent definition grants and costs: its tools, model, effort, CLAUDE.md, and hooks."""

    name: str
    path: Path
    tools: str
    model: str
    effort: str | None
    omits_claude_md: bool
    hooks: list[tuple[Hook, str]]


def unquoted(value: str) -> str:
    value = value.strip()
    return value[1:-1] if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'" else value


def agent_hooks(lines: list[str]) -> list[tuple[Hook, str]]:
    """Each command hook a definition's hooks value declares, with its matcher, read from the value's lines: an
    event's key, a matcher group's item, and the keys of each hook in the group. A group without a matcher matches
    every tool call; a command written as a block scalar starts the program its first line names."""
    hooks: list[Hook] = []
    matchers: dict[int, str] = {}
    event, group, base = "", 0, None
    block: int | None = None
    for raw in lines:
        text = raw.strip()
        indent = len(raw) - len(raw.lstrip(" "))
        if not text or text.startswith("#"):
            continue
        if block is not None:
            if indent > block:
                if hooks and hooks[-1].program == "unknown":
                    hooks[-1].program = unquoted(text.split()[0])
                continue
            block = None
        match = HOOK_KEY.match(text)
        if match is None:
            continue
        item, key, value = bool(match.group(1)), match.group(2), unquoted(match.group(3) or "")
        base = indent if base is None else base
        if indent == base and not item:
            event = key
        elif item and key in ("matcher", "hooks"):
            group += 1
            matchers[group] = value if key == "matcher" else "*"
        elif key == "matcher":
            matchers[group] = value
        else:
            if item:
                hooks.append(Hook(event, group))
            if hooks and hook_field(hooks[-1], key, value):
                block = indent
    return [(hook, matchers.get(hook.group, "*")) for hook in hooks]


def hook_field(hook: Hook, key: str, value: str) -> bool:
    """Record one key of a hook; True when its command is a block scalar, whose first line names the program."""
    if key == "command" and BLOCK_SCALAR.fullmatch(value):
        return True
    if key == "command" and value:
        hook.program = unquoted(value.split()[0])
    elif key == "timeout":
        hook.timeout = value
    elif key == "onFailure":
        hook.on_failure = value
    return False


def read_agent(path: Path, name: str | None = None) -> AgentDefinition | str:
    """An agent definition, under the name a skill names it by, or why it cannot be read."""
    try:
        document = frontmatter.read(path)
        tools_value = document.value("tools")
        model = document.string("model")
        effort = document.string("effort")
        omits = (document.string("omitClaudeMd") or "").strip().casefold() == "true"
        hook_lines = document.lines("hooks") or []
    except (OSError, UnicodeError, frontmatter.FrontmatterError) as exc:
        return str(exc)
    listed = tools_value if isinstance(tools_value, list) else re.split(r"[,\s]+", tools_value or "")
    tools = ",".join(name.strip() for name in listed if name.strip()) or "all"
    model = (model or "none").strip()
    return AgentDefinition(name or path.stem, path, tools, model, effort, omits, agent_hooks(hook_lines))


def delegates(line: str) -> bool:
    """Whether a prose line names a subagent or the tool that starts one."""
    return bool(DELEGATION.search(line) or line_references(line, None, ["Agent", "Task"], []))


def skill_markdown(folder: Path) -> list[tuple[Path, list[str]]]:
    """A skill folder's SKILL.md and every doc file inventory lists, each with its lines."""
    return [(path, text.splitlines()) for path in scanned_files(folder) if (text := read_text(path)) is not None]


def prose_lines(lines: list[str]) -> list[int]:
    """The indexes of a Markdown file's body lines outside fences."""
    languages, _ = parse_fences(lines)
    return [index for index in range(body_start(lines), len(lines)) if languages[index] is None]


def agents_folder(folder: Path) -> Path:
    """Where the agent definitions sit beside a skill's root: agents/ in a source tree, ~/.claude/agents for a
    deployed skill, and a project's .claude/agents for one of its skills."""
    return folder.parent.parent / AGENTS_FOLDER


def defined_agents(folder: Path) -> dict[str, Path]:
    """Each agent definition beside the skill's root, by the name a skill names it by: in a plugin, the plugin's name,
    a colon, and the agent's, as Claude Code namespaces a plugin's agents."""
    root = agents_folder(folder)
    plugin = plugin_of(folder)
    prefix = f"{plugin[0]}:" if plugin is not None else ""
    return {f"{prefix}{path.stem}": path for path in sorted(root.glob("*.md"))} if root.is_dir() else {}


def line_agents(line: str, defined: set[str]) -> list[str]:
    """The subagent types a prose line names: a type a delegation line names by TYPED_AGENT, and any code span that is
    the name of a defined agent."""
    found = [name for match in TYPED_AGENT.finditer(line) for name in match.groups() if name] if delegates(line) else []
    return found + [span.strip() for span in CODE_SPAN.findall(line) if span.strip() in defined]


def named_agents(folder: Path, markdown: list[tuple[Path, list[str]]]) -> dict[str, tuple[Path, int]]:
    """Each subagent type the skill's Markdown names, with the file and line that first names it."""
    defined = set(defined_agents(folder))
    found: dict[str, tuple[Path, int]] = {}
    for path, lines in markdown:
        for index in prose_lines(lines):
            for name in line_agents(lines[index], defined):
                found.setdefault(name, (path, index + 1))
    return found


def field_text(value: str) -> str:
    """A value a definition or hooks file gives, as one field of a printed line: flattened, with no whitespace left
    inside it, so no value can end its line or read as two fields."""
    return flat_text(value).replace(" ", "")


def hook_text(hook: Hook, matcher: str) -> str:
    """A hook's event, matcher, program, and limits, as AGENT_HOOK and PLUGIN_HOOK print them."""
    event, program = field_text(hook.event), field_text(hook.program)
    limits = f"timeout={field_text(hook.timeout)} on_failure={field_text(hook.on_failure)}"
    return f"{event} {field_text(matcher)} {program} {limits}"


def definition_lines(definition: AgentDefinition) -> list[str]:
    name = field_text(definition.name)
    return [
        f"AGENT {name} {posix(definition.path)}",
        f"AGENT_TOOLS {name} {field_text(definition.tools)}",
        f"AGENT_MODEL {name} {field_text(definition.model)}",
        f"AGENT_CLAUDE_MD {name} {'omitted' if definition.omits_claude_md else 'loaded'}",
        *(f"AGENT_HOOK {name} {hook_text(hook, matcher)}" for hook, matcher in definition.hooks),
    ]


def json_hook(event: str, group: int, handler: dict[str, object]) -> Hook:
    """One command handler of a hooks file, read as agent_hooks reads a definition's."""
    hook = Hook(event, group)
    command = handler.get("command")
    if isinstance(command, str) and command.split():
        hook.program = unquoted(command.split()[0])
    hook.timeout = str(handler["timeout"]) if "timeout" in handler else hook.timeout
    hook.on_failure = str(handler["onFailure"]) if "onFailure" in handler else hook.on_failure
    return hook


def plugin_hooks(folder: Path) -> list[tuple[Hook, str]]:
    """Each command hook the hooks file of the skill's plugin holds, with its matcher; none outside a plugin. A hooks
    file that cannot be read, is not JSON, or is not a JSON object raises InputError."""
    plugin = plugin_of(folder)
    path = plugin[1] / skill_menu.PLUGIN_HOOKS if plugin is not None else None
    if path is None:
        return []
    try:
        if not is_present(path):
            return []
    except OSError as exc:
        raise InputError(f"cannot read {posix(path)}: {exc.strerror or exc}") from exc
    try:
        document = json_reader.loads(require_text(path))
    # ValueError covers a decode error, UnicodeError, and an over-long integer; RecursionError, nesting too deep
    except (ValueError, RecursionError) as exc:
        raise InputError(f"{posix(path)} is not JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise InputError(f"{posix(path)} is not a JSON object")
    events = document.get("hooks")
    found: list[tuple[Hook, str]] = []
    group = 0
    for event, groups in events.items() if isinstance(events, dict) else []:
        for entry in groups if isinstance(groups, list) else []:
            group += 1
            matcher = entry.get("matcher") if isinstance(entry, dict) else None
            handlers = entry.get("hooks") if isinstance(entry, dict) else None
            found += [
                (json_hook(event, group, handler), matcher if isinstance(matcher, str) and matcher else "*")
                for handler in (handlers if isinstance(handlers, list) else [])
                if isinstance(handler, dict) and handler.get("type") == "command"
            ]
    return found


def started_agents(folder: Path, markdown: list[tuple[Path, list[str]]]) -> list[tuple[str, Path, int, object]]:
    """Each subagent type the skill names other than a built-in one, with where it is first named and its definition:
    an AgentDefinition, the reason it cannot be read, or None when no definition exists."""
    definitions = defined_agents(folder)
    found: list[tuple[str, Path, int, object]] = []
    for name, (path, line) in named_agents(folder, markdown).items():
        if name in BUILTIN_AGENTS:
            continue
        definition = definitions.get(name)
        found.append((name, path, line, read_agent(definition, name) if definition is not None else None))
    return found


def agent_lines(folder: Path) -> list[str]:
    """What each subagent type a skill names grants and costs, a type with no definition or, in a source tree, one no
    declared dependency installs, each hook its plugin runs in every session, and a general-purpose subagent started
    where a guarded type is at hand."""
    markdown = skill_markdown(folder)
    declared = declared_agents(folder)
    output: list[str] = []
    guarded: list[str] = []
    for name, path, line, definition in started_agents(folder, markdown):
        if definition is None:
            output.append(f"AGENT_UNDEFINED {posix(path)} {line} {name}")
            continue
        if declared is not None and name not in declared:
            output.append(f"AGENT_UNDECLARED {posix(path)} {line} {name}")
        if isinstance(definition, AgentDefinition):
            output += definition_lines(definition)
            guarded += [name] if definition.hooks else []
        else:
            output.append(f"AGENT_UNREADABLE {posix(defined_agents(folder)[name])} {definition}")
    for name in declared or {}:
        path = agents_folder(folder) / f"{name}.md"
        definition = read_agent(path) if path.is_file() and name not in guarded else None
        guarded += [name] if isinstance(definition, AgentDefinition) and definition.hooks else []
    defined = set(defined_agents(folder))
    hooks = plugin_hooks(folder)
    output += [f"PLUGIN_HOOK {hook_text(hook, matcher)}" for hook, matcher in hooks]
    # The plugin build moves each agent's own hooks into the plugin's hooks file, so they guard every agent it carries.
    guarded += [name for name in defined if name not in guarded] if hooks else []
    for path, lines in markdown:
        for index in prose_lines(lines):
            text = lines[index]
            typed = [name for name in line_agents(text, defined) if name not in BUILTIN_AGENTS]
            if guarded and GENERAL_PURPOSE.search(text) and delegates(text) and not typed:
                output.append(f"UNGUARDED_SUBAGENT {posix(path)} {index + 1} {guarded[0]}")
    return output


# --- subagent models and efforts ------------------------------------------------------------------


def sentences(line: str) -> list[str]:
    """A prose line's sentences and clauses: split after a full stop, question or exclamation mark, or semicolon
    before the next one's capital, code span, emphasis, or parenthesis."""
    return re.split(r"(?<=[.!?;])\s+(?=[A-Z`*(])", line)


def stated_settings(markdown: list[tuple[Path, list[str]]]) -> list[tuple[str, Path, int, str, bool]]:
    """Each model and effort the skill's Markdown starts a subagent on: the value of a printed line it relays, or a
    literal it names, as (kind, file, line, value, literal)."""
    found: list[tuple[str, Path, int, str, bool]] = []
    for path, lines in markdown:
        for index in prose_lines(lines):
            for sentence in sentences(lines[index]):
                if not DELEGATION.search(sentence):
                    continue
                for word in (match.group(1) for match in STATUS_SPAN_WORD.finditer(sentence)):
                    found += [
                        (kind, path, index + 1, f"printed {word}", False)
                        for kind in SETTING_WORDS
                        if kind.upper() in word
                    ]
                if START_VERB.search(sentence):
                    found += [
                        ("model", path, index + 1, value.casefold(), True) for value in LITERAL_MODEL.findall(sentence)
                    ]
                    found += [
                        ("effort", path, index + 1, value.casefold(), True)
                        for value in LITERAL_EFFORT.findall(sentence)
                    ]
    return list(dict.fromkeys(found))


def setting_kind(key: str, value: object) -> str | None:
    """Whether a key and its value set a subagent's model or effort: a model alias or ID, or an effort level."""
    kind = SETTING_KEYS.get(key)
    if not isinstance(value, str) or kind is None:
        return None
    if kind == "model":
        return kind if MODEL_VALUE.fullmatch(value.strip()) else None
    return kind if value.strip().casefold() in EFFORT_LEVELS else None


def json_settings(path: Path, text: str) -> list[tuple[str, Path, int, str]]:
    """Each model or effort a JSON data file sets, such as a manifest's per-role model or a configured effort."""
    try:
        data = json_reader.loads(text)
    # ValueError covers a decode error, UnicodeError, and an over-long integer; RecursionError, nesting too deep
    except (ValueError, RecursionError):
        return []
    found: list[tuple[str, Path, int, str]] = []
    stack: list[object] = [data]
    while stack:
        node = stack.pop()
        items = node.items() if isinstance(node, dict) else enumerate(node) if isinstance(node, list) else []
        for key, value in items:
            kind = setting_kind(key, value) if isinstance(key, str) else None
            if kind is not None and isinstance(key, str) and isinstance(value, str):
                where = re.search(rf'"{re.escape(key)}"\s*:\s*"{re.escape(value)}"', text)
                found.append((kind, path, text.count("\n", 0, where.start()) + 1 if where else 1, value))
            elif isinstance(value, dict | list):
                stack.append(value)
    return sorted(found, key=lambda item: item[2])


def python_settings(path: Path, text: str) -> list[tuple[str, Path, int, str]]:
    """Each model or effort a script sets as a literal: a dict entry or a keyword argument named for one."""
    try:
        tree = ast.parse(text)
    # ValueError covers, before 3.12, a null byte; MemoryError and RecursionError, source nested past the parser's
    # limits
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return []
    pairs: list[tuple[str, ast.expr, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            pairs += [
                (key.value, value, key.lineno)
                for key, value in zip(node.keys, node.values, strict=True)
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            ]
        elif isinstance(node, ast.keyword) and node.arg is not None:
            pairs.append((node.arg, node.value, node.value.lineno))
    return sorted(
        (
            (kind, path, line, value.value)
            for key, value, line in pairs
            if isinstance(value, ast.Constant)
            and isinstance(value.value, str)
            and (kind := setting_kind(key, value.value)) is not None
        ),
        key=lambda item: item[2],
    )


def dependency_folders(folder: Path, markdown: list[tuple[Path, list[str]]]) -> list[Path]:
    """The folders of the skills this one depends on: the skill_deps its metadata reaches in a source tree, and each
    sibling its Markdown reaches through ${CLAUDE_SKILL_DIR}/../."""
    root = source_root(folder)
    found = skill_closure(root, folder.name)[1:] if root is not None else []
    found += [name for _, lines in markdown for line in lines for name in SIBLING_SKILL.findall(line)]
    return [
        folder.parent / name for name in dict.fromkeys(found) if name != folder.name and (folder.parent / name).is_dir()
    ]


def data_settings(folders: list[Path]) -> list[tuple[str, Path, int, str]]:
    """The models and efforts the skill's and its dependencies' files set for a subagent: a reference's frontmatter, as
    a reviewer profile's, a JSON data file, or a script's literal."""
    found: list[tuple[str, Path, int, str]] = []
    for folder in folders:
        for path in folder_files(folder):
            relative = path.relative_to(folder)
            text = read_text(path)
            kind = category(relative)
            if text is None or kind == "main" or is_suite_name(path.name):
                continue
            if kind == "doc":
                found += markdown_settings(path, text)
            elif path.suffix.casefold() == ".json":
                found += json_settings(path, text)
            elif path.suffix.casefold() == ".py":
                found += python_settings(path, text)
    return found


def markdown_settings(path: Path, text: str) -> list[tuple[str, Path, int, str]]:
    """The model and effort a Markdown file's frontmatter sets, such as a reviewer profile's."""
    lines = text.splitlines()
    try:
        found = frontmatter.split(lines)
        document = frontmatter.Frontmatter(found[0] if found else [])
        values = {key: document.string(key) for key in ("model", "effort") if key in document}
    except frontmatter.FrontmatterError:
        return []
    settings = []
    for key, value in values.items():
        number = next((index + 1 for index, line in enumerate(lines) if line.startswith(f"{key}:")), 1)
        if (kind := setting_kind(key, value)) is not None and value is not None:
            settings.append((kind, path, number, value))
    return settings


def setting_lines(folder: Path) -> list[str]:
    """Where each model and effort a subagent of the skill starts on comes from, and each literal one it pins."""
    markdown = skill_markdown(folder)
    stated = stated_settings(markdown)
    output = [
        f"SUBAGENT_MODEL {posix(path)} {line} {value}"
        if kind == "model"
        else f"SUBAGENT_EFFORT {posix(path)} {line} {value}"
        for kind, path, line, value, _ in stated
    ]
    pinned = [(kind, path, line, value) for kind, path, line, value, literal in stated if literal]
    for _, _, _, definition in started_agents(folder, markdown):
        if isinstance(definition, AgentDefinition):
            pinned += markdown_settings(definition.path, require_text(definition.path))
    pinned += data_settings([folder, *dependency_folders(folder, markdown)])
    output += [
        f"PINNED_SUBAGENT {posix(path)} {line} {kind} {value}"
        for kind, path, line, value in dict.fromkeys(pinned)
        if value.strip().casefold() != "inherit"
    ]
    return output


# --- runtime support ------------------------------------------------------------------------------


def bash_git_subcommands(text: str) -> set[str]:
    """The subcommand of each git command a Bash script runs where a command starts, outside comments."""
    found: set[str] = set()
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        for match in GIT_COMMAND.finditer(line):
            subcommand = git_client.subcommand([word.strip("\"'") for word in match.group(1).split()])
            found |= {subcommand} if subcommand else set()
    return found


def script_git_subcommands(script: Path) -> set[str]:
    """The subcommand of each git command a Bash script runs, or of each git call a Python script makes, by the
    shared derivation; any other script runs none this reads. A Python script that does not parse fails, as
    script_facts fails it."""
    text = require_text(script)
    suffix = script.suffix.casefold()
    if suffix in (".sh", ".bash"):
        return bash_git_subcommands(text)
    if suffix != ".py":
        return set()
    try:
        return runtime_support.python_git_subcommands(text)
    # ValueError covers, before 3.12, a null byte; MemoryError and RecursionError, source nested past the parser's
    # limits
    except (SyntaxError, ValueError, MemoryError, RecursionError) as exc:
        raise InputError(f"cannot read {posix(script)}: not valid Python") from exc


def needs_evidence(folder: Path, granted: list[str], user_only: bool, tools: list[str]) -> list[tuple[str, str]]:
    """Each runtime capability the skill needs, by the shared derivation, with what shows it: a grant, user-only
    start, a hooked agent it starts, a network tool it declares, or a git subcommand that reaches a remote."""
    evidence = [
        (need, f"grant {grant}") for grant in granted for need in runtime_support.frontmatter_needs([grant], False)
    ]
    evidence += [(need, "user-only") for need in runtime_support.frontmatter_needs([], user_only)]
    for name, _, _, definition in started_agents(folder, skill_markdown(folder)):
        if isinstance(definition, AgentDefinition) and definition.hooks:
            hooked = runtime_support.frontmatter_needs(granted, False, starts_hooked_agent=True)
            evidence += [(need, f"agent {name}") for need in hooked - runtime_support.frontmatter_needs(granted, False)]
    for tool in tools:
        evidence += [
            (need, f"tool {tool}") for need in runtime_support.frontmatter_needs([], False, tools=frozenset({tool}))
        ]
    for script in folder_scripts(folder / "scripts"):
        for subcommand in sorted(script_git_subcommands(script)):
            needs = runtime_support.frontmatter_needs([], False, git_subcommands=frozenset({subcommand}))
            evidence += [(need, f"git {subcommand} {posix(script.relative_to(folder.resolve()))}") for need in needs]
    return list(dict.fromkeys(evidence))


def own_needs(folder: Path) -> set[str]:
    """The runtime capabilities a skill's own frontmatter, metadata, and scripts show it needs, as needs_evidence reads
    them for the skill audited."""
    path = folder / "SKILL.md"
    try:
        document = frontmatter.read(path)
        granted = parse_allowed_tools(document.value("allowed-tools")) or []
        listed = listing(document)
    except (OSError, UnicodeError, frontmatter.FrontmatterError) as exc:
        raise InputError(f"cannot read {posix(path)}: {exc}") from exc
    metadata = read_metadata(folder.parent.parent, folder.name)
    tools = names(metadata, "tools") + names(metadata, "optional_tools")
    return {need for need, _ in needs_evidence(folder, granted, "INVOCATION model" not in listed, tools)}


def invoked_evidence(folder: Path) -> list[tuple[str, str]]:
    """Each runtime capability a skill the audited one invokes carries over to it, with that skill, by
    ejryan89-skill-core's runtime_support derivation, which repository validation shares."""
    skills = folder.parent
    known = {path.parent.name for path in skills.glob("*/SKILL.md")}

    def invokes(name: str) -> set[str]:
        path = skills / name / "SKILL.md"
        return runtime_support.invoked_skills(require_text(path), known) if name in known else set()

    carried = runtime_support.carried_needs(folder.name, lambda name: own_needs(skills / name), invokes)
    return [
        (need, f"skill {name}")
        for name, needs in carried.items()
        for need in runtime_support.CAPABILITIES
        if need in needs
    ]


def runtime_lines(folder: Path, granted: list[str], user_only: bool) -> list[str]:
    """In a source tree, each runtime's declared support, what the skill needs and why, and each runtime declared
    full that lacks a capability the skill needs."""
    root = source_root(folder)
    metadata = read_metadata(root, folder.name) if root is not None else None
    if metadata is None:
        return []
    if "runtime_support" not in metadata:
        return ["RUNTIME_UNDECLARED"]
    try:
        declared = runtime_support.parse(metadata["runtime_support"])
    except runtime_support.RuntimeSupportError as exc:
        return [f"RUNTIME_UNREADABLE {exc}"]
    evidence = needs_evidence(folder, granted, user_only, names(metadata, "tools") + names(metadata, "optional_tools"))
    evidence += invoked_evidence(folder)
    needs = {need for need, _ in evidence}
    output = [
        f"RUNTIME {runtime} {support.level} needs={','.join(support.needs) or 'none'}"
        + (f" {support.reason}" if support.reason else "")
        for runtime, support in declared.items()
    ]
    output += [f"RUNTIME_NEEDS {need} {shown}" for need, shown in evidence]
    output += [
        f"RUNTIME_OVERCLAIMED {runtime} {','.join(lacking)}"
        for runtime, support in declared.items()
        if support.level == runtime_support.FULL and (lacking := runtime_support.lacking(runtime, needs))
    ]
    return output


# --- provenance -----------------------------------------------------------------------------------


def provenance(names: list[str], path: Path | None = None) -> list[str]:
    """What each named check rests on, from the skill's provenance file, and NOT_A_CHECK for a name that is a
    measurement, a location, or no line at all."""
    file = path if path is not None else Path(__file__).resolve().parents[1] / check_provenance.PROVENANCE_FILE
    try:
        sources = check_provenance.load(file, CHECKS)
    except check_provenance.ProvenanceError as exc:
        raise InputError(str(exc)) from exc
    return [
        line
        for name in dict.fromkeys(names)
        for line in (check_provenance.lines(sources, name) if name in CHECKS else [f"NOT_A_CHECK {name}"])
    ]


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    find = commands.add_parser("locate", help="find a skill's main file")
    find.add_argument("name", nargs="?", default="", help="[<plugin>:]<skill>, then optionally a plugin folder")
    find.add_argument("--repo", type=Path, help="project root; default: the Git root of the current directory")
    find.add_argument("--home", type=Path, default=Path.home())
    commands.add_parser("inventory", help="size every file of a skill").add_argument("directory", type=Path)
    commands.add_parser("tools", help="compare referenced tools with allowed-tools").add_argument("file", type=Path)
    commands.add_parser("scan", help="list cost cues").add_argument("paths", type=Path, nargs="+")
    commands.add_parser("provenance", help="name the guidance a check derives from").add_argument("checks", nargs="+")
    args = parser.parse_args(arguments)
    try:
        if args.command == "locate":
            repo = args.repo if args.repo is not None else git_toplevel(Path.cwd())
            # The argument as the user gave it: a name, then a plugin folder after the first space.
            name, _, folder = args.name.strip().partition(" ")
            output = locate(name, args.home, repo, Path(folder.strip()) if folder.strip() else None)
        elif args.command == "inventory":
            output = inventory(args.directory)
        elif args.command == "tools":
            output = tools(args.file)
        elif args.command == "provenance":
            output = provenance(args.checks)
        else:
            output = scan(args.paths)
    except InputError as exc:
        print(single_line(f"FAILED {exc}"))
        return 1
    except OSError as exc:
        where = f" {Path(exc.filename).as_posix()}" if exc.filename else ""
        print(single_line(f"FAILED cannot read{where}: {exc.strerror or exc}"))
        return 1
    # A line names paths, names, and values the audited files give; none of them may end the line it sits on.
    output = [single_line(line) for line in output]
    print("\n".join(output))
    return 1 if any(line.startswith(("STOP ", "UNREADABLE ", "NOT_A_CHECK ")) for line in output) else 0


if __name__ == "__main__":
    run_main(main)
