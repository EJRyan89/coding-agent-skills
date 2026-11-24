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
prints only that line for an empty name. A command that cannot finish prints FAILED <reason>, naming why, as its last
line.

Exit status:
  0  the command printed its facts
  1  a STOP, UNREADABLE, or NOT_A_CHECK line, or a last line FAILED <reason>
  2  a usage error

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

Files: inventory, scan, and the model and effort reading take a skill folder's files as ejryan89-skill-core's
folder_walk gives them, passing over its skipped folders and nested checkouts, and add each Markdown file in the folder
that the skill's own Markdown links, directly or through another file so linked, wherever it lies, since the runtime
loads it all the same. A skill folder whose files the audit measures, the one examined or one it depends on or invokes,
is read through ejryan89-skill-core's tree_reader, a reader bound to that folder, which judges each path by its text and
its links before touching it: a file or folder the folder holds through a link that leads out of it is passed over
unread, never followed, a script a command runs through one included; a path it refuses otherwise ends the command with
FAILED <path> cannot be examined: <reason>. A Markdown file it loads that is larger than the reader's bound ends it with
FAILED cannot read <path>: <reason>, and a helper or data file so large is sized from its status and counts 0. A folder
or file the user names through a `..` is resolved before a reader is bound to it, and named so. The reads the audit
makes outside the folder by design are each stated in TREE_READ_ALLOWED with why. A linked path that leads out of the
folder, through `..` at its start or later or through a link, to a folder or a file, that leads out of it, is an outside
read, OUTSIDE_READ or OUTSIDE_MISSING, and so is an absolute one. A link's destination is read as CommonMark reads it,
through ejryan89-skill-core's link_destinations: percent-decoded, between < and > with spaces, or with a backslash, read
as the platform reads a path: a separator on Windows, part of the name elsewhere. A Markdown path named in prose or a
code span is read as written, from where it starts to its `.md`. It starts at a line's start, after a blank, `(`, `[`,
or `<`, or after a quote or a backtick that follows no path character, and a relative one also after an emphasis `*`
that follows none and closes right after the `.md`, as in `*a.md*`; it may begin with ${CLAUDE_SKILL_DIR}/, and an
absolute one opens with its root, `/`, `\\`, or a drive such as `C:\\`. Its first character, or the one after the root,
is a word character, `.`, `%`, or `-`; the rest are those, `/`, `\\`, `+`, `@`, `~`, or `&`; and it ends at `.md` that
is followed by none of those characters other than `.`, and by no `.` followed by one. Its `%` is part of the name, its
backslash is read as a link's is, and an absolute one is an outside read. So a path holding another character, such as
`'`, `=`, `#`, `;`, `,`, `:`, or `!`, is not read, nor is an absolute one that starts anywhere else, as after `=` in
`--doc=/abs/a.md`; and since prose separates words with spaces, a path holding one, such as `/opt/my notes/a.md`, is
read from after its last space. In a shell or PowerShell fence below the frontmatter, a path is read only where a file
command, one of SHELL_FILE_COMMANDS (cat, head, tail, find, grep, rg, sed, awk, wc, or ls), reads it for the model. A
fence BASH_FENCES labels that the one Bash grammar reads is read whole: a path in a word, or in a `<` redirection's
target, of a simple command that runs one, wherever Bash starts it and through a wrapper, eval, or a shell's -c too. A
console, fish, cmd, or PowerShell fence, and a Bash fence the grammar refuses, a here-document in it runs to its end of,
or in which it finds a command whose program, past any assignments before it, is an unquoted `$` alone, a prompt, are
read by command line, such a Bash fence one of whose lines starts with `$ ` as a console session is, its commands the
prompted lines and the lines that continue them: a path on a line whose first word, after its prompt, names one, as
scan's shell-file-command cue reads it, from that word to the line's first `>`, `|`, `;`, or `&`, which ends the
command's own words in each of these shells. Any other path such a fence names, such as a redirection's target in `>
../log.md`, `Out-File ../w.md` after a pipe, an option's operand in `gh pr create --body-file /tmp/b.md`, a script's
argument, a here-document's line, or a session's output, is one the command writes or hands its program at run time,
which the skill does not load. An untagged fence, and one of any other language, is read as prose is. Limits, which no
skill's Markdown in this source writes: a path followed right away by `&`, as in `a.md&nbsp;` or `?f=a.md&x=1`, is not
read, and `a.md&b.md` is read as one path; a path after an HTML entity, as in `&lt;a.md&gt;`, is not read; nor is a UNC
path such as `\\\\server\\share\\a.md`, a path that starts with `~`, `$HOME`, or `${VAR}`, or
`${CLAUDE_SKILL_DIR}\\..\\a.md`; a Markdown backslash escape in prose is not taken out of the name; a path that prose or
a code span names is read though the skill may only write it, as in `write the body to /tmp/b.md`; a path a file command
writes itself is read, as in awk's `print > "../a.md"`, sed's `w ../a.md` or `-i`, or find's `-fprint ../a.md`; in Bash
a path reaches a file command only through its own words, so `for f in ../a.md; do cat "$f"; done`, `echo ../a.md |
xargs cat`, and a program SHELL_FILE_COMMANDS does not name, such as diff or less, read none, and a path in a `<`
redirection's target of a compound command, as in `{ cat; } < a.md`, is not read; read by command line, a file command
that is not the line's first word reads none, as in PowerShell's `$x = cat ../a.md`, `& cat ../a.md`, `gci | cat`, or
`$x = 1; cat ../a.md`, or a session's `ls | grep x ../a.md`, nor do PowerShell's gc, Get-Content, Select-String, or sls,
or cmd's type, a `>`, `|`, `;`, or `&` inside quotes ends the line's reading too, as in `grep 'a|b' ../a.md`, and so
does a redirection before the file command's operands, as in `cat 2> err.log ../a.md`, and a path on a line that
continues the command before it is not read, as `../a.md` after `cat \\`; and only the inventory reads a Bash fence
written with `$ ` prompts as a session: scan's cue reads it through the grammar, which takes the `$` for the program,
and the grant checks read each line's text, its `$ ` included. The measure, NESTED_REFERENCE, and the outside reads all
read the paths a file names this one way.

Token estimate: ceil(prose_chars / 4 + code_chars / 3), in characters of the UTF-8 text. Every
character of a helper script or data file is code. In Markdown, lines inside fenced code blocks,
including the fence lines, are code and the rest is prose. A helper script or data file that is not UTF-8 text
counts 0, since it is measured rather than loaded. A Markdown file the skill loads, its own or one it reads from
outside its folder, that is not UTF-8 text, or one outside its folder that cannot be examined, prints
UNREADABLE <path> <reason> instead of a cost, the path relative to the skill folder, and inventory exits 1 after it.
tools and scan read the text itself and no Markdown outside the folder: tools fails on a Markdown file in the folder
that is not UTF-8 text, and scan on one it is given or that a folder it is given stands for. scan fails too on a script
whose status lines it compares that is not UTF-8 text, not valid Python, Bash the one Bash grammar refuses, or larger
than the reader's bound, whose output it cannot measure, and tools on a script a command of the skill runs, or one of
the skill's own whose git commands it reads in a source tree, that is larger than the reader's bound.
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
whether the description starts the skill, TRIGGER_SCENARIO_UNREADABLE <path> <reason> each scenario.json there the
audit cannot use, the reason naming why, and NO_TRIGGER_SCENARIO <path> names that folder when it holds neither.

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
code or subcommand the * supplies. A command is a fence command, each line of a shell or PowerShell fence that is
no comment, a console session's commands alone, as the Bash paragraph below reads them, and in a POSIX shell's, fish's,
or console fence a line a backslash continues joined with the next, as Bash joins them, on its first line; or a code
span outside a fence that runs a script: a program and a script's path as that rule reads them.
UNGRANTED <tool> <line> <command> is a command that no pattern of a shell with patterns matches, so running it through
that tool prompts. A pattern matches the command's text, quotes included, with * for any text, and one whose only *
ends it after a space also matches the bare command before it. A line that joins commands with &&, ||, ;, or | outside
quotes is judged one command at a time, as Claude Code checks it, and UNGRANTED names the command no pattern matches.
EXPANDS <line> <command> is a command line that expands a shell variable or command substitution other than
${CLAUDE_SKILL_DIR} or $ARGUMENTS, which prompts in both shells whatever is granted, so it is reported whether or not
the skill has allowed-tools. IDLE_ALLOWED <entry> is a scoped Bash or PowerShell pattern that matches no fence command
and no code span outside a fence, so it pre-approves a command the skill never asks for. HIDDEN_ALLOWED is a hidden
skill with any grant: no turn starts it, so its grants approve nothing, and it takes the place of its IDLE_ALLOWED
lines.
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

Bash: scan reads a fence BASH_FENCES labels whole through ejryan89-skill-core's Bash grammar, as Bash reads it, each
line as CommonMark gives it to the fence's content, and places the shell-file-command cue on the line each command
that runs a file command starts on, so a here-document's body, a comment, and a quoted string run nothing, and a
command after a continuation is placed on its own line; an untagged fence is read so when the grammar reads it, and
holds no Bash otherwise. A fence of any other shell, console, fish, cmd, or PowerShell, places the cue on each line
whose first word, after its prompt, names a file command, in any case in a cmd or PowerShell fence and as written in a
console or fish fence, as it reads a Bash fence. A console fence that holds a prompt line is a session, which both the
cue and the grant checks read on its prompted lines and the lines that continue them, after a backslash or with a `> `
prompt, without the prompt, the rest being output. A prompt takes a form Pygments' console lexer reads, `$`, `%`, or
U+276F, or one of those or `#` after a user and host, as `user@host:~$`, a bracketed one, as `[root@host dir]#`, or
`sh-5.2`, any of them after a virtual environment, as `(venv) `, and then a space; a bare `# ` starts a comment, and
session_commands names where the two readings differ. A console fence
with no prompt line is read whole, and another shell's fence on every line, after any `$ ` or `> ` prompt. A fence of
any other language holds none. A Bash script given to scan is read whole the
same way. BASH_UNREADABLE <file> <line> <reason> is such a labelled fence, opened at that line, or a script, at its
first, that the grammar refuses, as Bash would, or a fence a here-document is still open at the end of, none of whose
commands are read.

Deterministic prose: besides the cost cues, scan names prose a script should replace, each a CUE line:
assembled-arguments (a flag chosen per printed line or value), parsed-output (output split, grouped, or renamed by a
fixed rule, or a printed value taken apart), carried-state (a count, summary, or last-seen value carried between
steps), owed-by-rules (the regenerations, gates, or follow-ups a change owes, decided by listed rules), and
hand-drafted (a file or message written by hand from a template or from printed lines).

Status lines: for a skill folder, scan reads the skill's own scripts, every script its Markdown's commands run, through
${CLAUDE_SKILL_DIR} or from the repository root (the nearest folder above with a .git entry), the scripts beside each of
those, and ejryan89-skill-core's; it skips regression suites, and passes over a script named in the skill folder that a
link leads out of, as the Files paragraph says. A status word is an upper-case name of three or more characters, such as
SKILL_FILE. A script prints every status word its strings hold, outside docstrings, and the FAILED line
ejryan89-skill-core's run_main adds; it prints a line starting with a word when the word leads an f-string, a string a
print, write, append, insert, extend, emit, or echo call takes, a listed string, or a string of a word, a space, and
more. It exits 0, a code an int-returning function returns or an exit call takes, 1 and 130 with run_main, and 2 with
argparse; a code is a literal or a name a constant binds, which ejryan89-skill-core's exit_constants reads as validation
does, an int literal bound at the module's top or in a module-level block, annotated or not, or one an import names in a
script the scan reads, from the script's own folder or ejryan89-skill-core's. A name no such reading reaches is an
unread exit, never one the script does not take; a local or a parameter is not read. A Bash script, read through the
Bash grammar, holds the status words of its commands' words, its functions' names, and its here-documents' bodies, but
none of a comment's; prints a line starting with the word an echo or printf command's first word after its options
starts with; and exits with each code an exit command names, one in a trap's action included. A PowerShell script is
read line by line, outside comment lines. UNPRINTED <file> <line> <word> is a word that leads a code span on a prose
line about output (one that says print, line, output, report, ends with, or exit) that no script prints, unless "such
as" brings it in as an example. EXIT_MISMATCH <file> <line> <code> is an exit code the prose states that no script uses,
and EXIT_UNREAD <file> <line> <code> one when some script has an unread exit, which may give it. UNEXPLAINED <script>
<line> <word> is a line a script the Markdown runs starts with a word the Markdown never names, for a script in the
skill's folder or outside every skills folder, such as a repository tool; a sibling skill's script is that skill's to
explain. A skill with no script of its own that runs none prints none of these. SCRIPT_MISSING <file> <line> <script> is
a command that runs, through ${CLAUDE_SKILL_DIR} or from the repository root, a script that does not exist, so nothing
it would print is compared.

Runtime needs: in a skill source tree, tools derives what the skill needs through ejryan89-skill-core's
runtime_support, as repository validation does, and prints RUNTIME_NEEDS <capability> <evidence> for each: grant
<tool>, user-only, agent <name>, tool <name>, git <subcommand> <script>, or skill <name> for a skill it invokes that
needs it, without that skill's user-only start. It reads a Bash script through ejryan89-skill-core's Bash grammar, and
fails on one the grammar refuses, as on a Python script that does not parse, since the git commands of a script Bash
would refuse cannot be read.
"""

from __future__ import annotations

import argparse
import ast
import errno
import fnmatch
import os
import re
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import dropwhile, pairwise
from pathlib import Path, PureWindowsPath

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

import bash_syntax
import check_provenance
import command_wrappers
import dotnet_solution
import exit_calls
import exit_constants
import frontmatter
import git_subcommands
import json_reader
import powershell_commands
import python_reader
import runtime_support
import skill_menu
import whole_number
from agent_skills import is_skill_file
from bash_syntax import BashSyntaxError, Simple
from console import run_main
from dotnet_solution import FileRefused, lexical_absolute
from executable_locator import RelativeEnvironmentPath, absolute_home, program_name
from file_presence import NotADirectory, NotAFile, is_directory, is_present, is_there, listed
from flat_text import flat_text, single_line
from git_client import GitClient, GitError
from link_destinations import link_destinations
from markdown_sections import (
    ATX_HEADING,
    FENCE_LANGUAGES,
    LIST_MARKER,
    POWERSHELL_FENCES,
    SHELL_FENCES,
    Fence,
    code_spans,
    find_fences,
    prose_code_spans,
)
from module_assignments import assigned_names, sole_assignment
from path_containment import resolved_path, within
from path_keys import path_key
from powershell_startup import POWERSHELL_PROGRAMS, StartupCommand, startup_command
from python_names import import_aliases, qualified_name, type_parameter_names
from suite_names import is_suite_name
from text_lines import LINE_FEED, line_index, line_starts, text_lines
from tree_reader import TreeReader, TreeRefused

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
# The programs that read or list files, which a skill's own tools do instead.
SHELL_FILE_COMMANDS = ("cat", "head", "tail", "find", "grep", "rg", "sed", "awk", "wc", "ls")
# The fence labels scan reads as Bash, through the one Bash grammar: the POSIX shells', but not `console`, whose
# fence is a session's prompts and the output between them rather than a script.
BASH_FENCES = FENCE_LANGUAGES["shell"] - {"console"}
# The labels of every other shell's fence, whose lines a model runs through its Bash or PowerShell tool, where cat,
# grep, find, and ls, or their aliases, still read files: console's, fish's, cmd's, and PowerShell's. scan reads the
# first word of each line such a fence runs, after its blanks and prompt: a session's, as session_commands reads
# it, and otherwise any `$ ` or `> `.
FIRST_WORD_FENCES = (
    FENCE_LANGUAGES["shell"] | FENCE_LANGUAGES["fish"] | FENCE_LANGUAGES["cmd"] | FENCE_LANGUAGES["powershell"]
) - BASH_FENCES
# The FIRST_WORD_FENCES labels whose shell finds a command in any case: cmd's and PowerShell's. Bash, which runs a
# console fence's commands, and fish match a function, an alias, or a builtin as written, and find a program in any
# case only where the file system ignores case; scan reads their first word as written, as it reads a Bash fence.
ANY_CASE_FENCES = FENCE_LANGUAGES["cmd"] | FENCE_LANGUAGES["powershell"]
# The FIRST_WORD_FENCES labels of a shell session, a command after each prompt and its output on the lines between:
# console's. The other labels name a script, every line of which is a command.
SESSION_FENCES = frozenset({"console"})
PROMPT = re.compile(r"\A[ \t]*(?:[$>] )?")
# A session's prompt, which starts a command line, in the forms Pygments' console lexer reads, then a space: after the
# blanks and any virtual environment, as `(venv) `, either `$`, `%`, or U+276F alone, or one of those or `#` after a
# user and host, as `user@host:~$` or `MacBook:~ user$`, a bracketed one, as `[user@host dir]#`, or `sh-5.2`. A
# bare `# ` starts a comment, as it does in a fence of bare commands, and output, such as a configuration file's,
# often starts with one.
# A user and host and the word after them hold no prompt character, so `user@host:~$ ls # list` runs `ls # list`.
PROMPT_HOST = r"\[[^\]\n]*\]|[\w.-]+[@:][^\s$#%\u276f]+(?:[ \t]+[^\s$#%\u276f]+)?|sh-[\d.]+"
SESSION_PROMPT = re.compile(rf"\A[ \t]*(?:\([^)\n]*\)[ \t]*)?(?:(?:{PROMPT_HOST})[ \t]*[$#%\u276f]|[$%\u276f]) ")
# The prompt that makes a BASH_FENCES fence a session, as SESSION_PROMPT makes a console fence one: `$` alone, then a
# space, which starts no Bash command. The other forms SESSION_PROMPT reads are not taken there, since a Bash line
# such as `[ -d build ] # check` starts like a bracketed one.
BASH_PROMPT = re.compile(r"\A[ \t]*\$ ")
# Where a command line read by its first word stops naming the command's own words: a redirection, a pipe, or the
# next command, in a POSIX shell's, fish's, cmd's, and PowerShell's own syntax.
COMMAND_END = re.compile(r"[>|;&]")
# The prompt Bash shows on each line that continues a command.
CONTINUATION_PROMPT = re.compile(r"\A[ \t]*> ")
# A line a backslash continues, as Bash continues one ending in a backslash no other escapes.
CONTINUED_LINE = re.compile(r"(?<!\\)(?:\\\\)*\\\Z")
# The labels of a fence whose shell joins a line a backslash continues to the next: the POSIX shells', console's, and
# fish's. PowerShell continues a line with a backtick and cmd with a caret, which the grant checks do not join.
BACKSLASH_FENCES = FENCE_LANGUAGES["shell"] | FENCE_LANGUAGES["fish"]
# The suffixes of a script read as Bash, which scan reads when it is given one, as the status reader reads one.
BASH_SUFFIXES = frozenset({".sh", ".bash"})
SHELL_FILE_COMMAND = rf"(?:{'|'.join(SHELL_FILE_COMMANDS)})"
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
# sentence, or follows "then" or "and". A list item's marker and the space and the tab around it are read as
# markdown_sections reads them.
VALUE_SPAN = r"`(?!--)[^`\n]+`"
STATUS_SPAN = r"`[A-Z][A-Z0-9_]+[^`\n]*`"
FLAG_SPAN = r"`--[\w-]+[^`\n]*`"
INSTRUCTION = rf"(?:^[ \t]*(?:{LIST_MARKER})?[ \t]*|[.:;]\s+|,\s+(?=print\b)|\b(?:then|and)\s+)"
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
            rf"(?-i:^[ \t]*{LIST_MARKER}[ \t]+\*\*`[a-z][^`]*\s[^`]*`)|\breport (?:it|them|that|this) as {STATUS_SPAN}"
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
        "BASH_UNREADABLE",
        "BODY_OVER_500_LINES",
        "DESCRIPTION",
        "DOC_NO_TOC",
        "DUPLICATE_BLOCK",
        "EXIT_MISMATCH",
        "EXIT_UNREAD",
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
# The Bash builtins that print their words, and the option words each may take first, as `-n` or `-e`.
BASH_PRINTS = frozenset({"echo", "printf"})
PRINT_OPTION = re.compile(r"-\w+")
# A status word a printed word starts with, followed by a blank, a quote, `=`, or the word's end.
PRINTED_STATUS = re.compile(rf"({STATUS_WORD})(?=[\s\"'=]|$)")
STATUS_SCRIPT_SUFFIXES = frozenset({".py", ".sh", ".bash", ".ps1"})
SKILL_DIR_PREFIX = "${CLAUDE_SKILL_DIR}/"
# The calls that print a line or add one to the lines a command prints. Those that exit with a code are
# ejryan89-skill-core's exit_calls, which validation's exit tables read too.
PRINTING_CALLS = frozenset({"print", "write", "append", "insert", "extend", "emit", "echo"})
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
# Where agent definitions sit beside a skills folder, and the subagent types a session has without one: the built-in
# subagents of the sub-agents page (https://code.claude.com/docs/en/sub-agents), Explore, Plan, and the general-purpose
# agent, which has every tool, and the helper agents its "Other" table lists, claude, statusline-setup, and
# claude-code-guide. A type a delegation line names: "of type `x`", "`x` type", "`x` subagent", or subagent_type x.
AGENTS_FOLDER = "agents"
BUILTIN_AGENTS = frozenset({"general-purpose", "Explore", "Plan", "claude", "statusline-setup", "claude-code-guide"})
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
SETTING_KEYS = {
    "model": "model",
    "effort": "effort",
    "reasoning_effort": "effort",
    "reviewer_effort": "effort",
    "reviewer_model": "model",
}
MODEL_VALUE = re.compile(r"(?i)haiku|sonnet|opus|fable|claude-[\w.-]+")
EFFORT_LEVELS = frozenset({"minimal", "low", "medium", "high", "xhigh", "max"})
# A string key and its string value as a JSON file writes them, at every place one starts, overlapping places included,
# so one pass over the file finds where each setting is first written. No setting's key or value holds a `"`:
# SETTING_KEYS names none, and MODEL_VALUE and EFFORT_LEVELS take none, so the text between a pair's quotes is the
# setting's key and value exactly where the file writes them without an escape.
JSON_STRING_PAIR = re.compile(r'(?="([^"]*)"\s*:\s*"([^"]*)")')
# A sibling skill a skill's Markdown reaches through its own directory.
SIBLING_SKILL = re.compile(r"\$\{CLAUDE_SKILL_DIR\}/\.\./([A-Za-z0-9._-]+)/")
# A Markdown path named in prose or code, outside every link's destination, which ejryan89-skill-core's
# `link_destinations` reads instead: `references/a.md`, `.venv/a.md` in a hidden folder, `../shared/a.md`, one after
# ${CLAUDE_SKILL_DIR}/, or an absolute one, `/opt/a.md` or `C:\a.md`. It starts at one of these, and nowhere else: a
# line's start; a blank, `(`, `[`, or `<`; a quote or a backtick that follows no PATH_CHARACTER; or, for a relative
# one alone, an emphasis `*` that follows none and closes right after the `.md`, so `*a.md*` is read and the glob
# `*.instructions.md` is not. An absolute one opens with its root, `/`, `\`, or a drive such as `C:\`. Its first
# character, or the one after the root, is a word character, `.`, `%`, or `-`; the rest are those, `/`, `\`, `+`, `@`,
# `~`, or `&`, so `%` is read as written, as prose is not percent-encoded, and `\` is kept for `markdown_links` to read
# as the platform reads a path: a separator on Windows, part of the name elsewhere. It ends at `.md` that is followed
# by none of those characters other than `.`, and by no `.` followed by one. `markdown_links` resolves where it leads.
PATH_CHARACTER = r"[\w%+@~&./\\-]"
PROSE_REFERENCE = re.compile(
    rf"(?:(?<![^\s(\[<])|(?<=(?<!{PATH_CHARACTER})[`'\"])"
    rf"|(?<=(?<!{PATH_CHARACTER})\*)(?=(?:\$\{{CLAUDE_SKILL_DIR\}}/)?[\w.%-][\w%+@~&./\\-]*\.md\*))"
    r"(\$\{CLAUDE_SKILL_DIR\}/)?((?:[A-Za-z]:[/\\]|[/\\])?[\w.%-][\w%+@~&./\\-]*\.md)(?!\.?[\w%+@~&/\\-])"
)
CONTENTS_HEADING = re.compile(r"(?i)(?:table of )?contents")
SPAN_SHELL_COMMAND = re.compile(rf"^{SHELL_FILE_COMMAND}(?:\s|$)")

# Every file of a skill folder the audit reads, its walk, its listings, and its files' text, is read through
# ejryan89-skill-core's tree_reader, a reader bound to that folder, as the module docstring's Files paragraph states.
# These reads go elsewhere by design, each named with why; a function here reads only what its reason names.
TREE_READ_ALLOWED = {
    "require_kind": (
        "the skill folder inventory is given, the file tools is given, or locate's plugin folder: a path the user "
        "names, asked whether it is there before any reader is bound to it, since it chooses the folder",
        ("inventory", "search_roots", "tools"),
    ),
    "given_path": (
        "resolves a path the user names that holds a `..`, the skill folder, file, repository, home, or plugin folder, "
        "before any reader is bound to it, since it chooses the folder",
        ("main",),
    ),
    "is_given_folder": (
        "whether a path the user names to scan is a folder, which then has a reader bound to it",
        ("scan", "scanned_files", "scanned_texts"),
    ),
    "not_a_file": (
        "whether something other than a file is at a path the user names to scan, before reading it as named",
        ("scanned_texts",),
    ),
    "is_given_file": (
        "whether a file is at a path the user names to scan, read as named, the user's own choice",
        ("scanned_texts",),
    ),
    "outside_bytes": (
        "the bytes of a file outside the examined folder: a Markdown file the skill names outside it (OUTSIDE_READ), a "
        "file the user names to scan, a .agents/skills shim locate compares, a runtime prompt's sample and the source "
        "files it names, an agent definition or a plugin's hooks file beside the skill's root, and a script a command "
        "runs from the repository root or another skill, or ejryan89-skill-core's; a file of the folder is read "
        "through its reader",
        (
            "measure_sample",
            "named_bytes",
            "outside_lines",
            "outside_text",
            "plugin_hooks",
            "scanned_texts",
            "setting_lines",
        ),
    ),
    "is_outside_file": (
        "whether a file is at one of the places outside_bytes names, following its links, as the runtime reading it "
        "does",
        ("agent_lines", "measure_sample", "outside_lines", "outside_scripts"),
    ),
    "opened_path": (
        "resolves a path as the runtime opens it: the examined folder's own place and the folders above it, a file of "
        "the folder the reader let through, whose links stay inside, a folder whose scripts the status lines read, or "
        "a path the skill's Markdown names, which is how the audit learns that it leads out of the folder, an outside "
        "read it reports",
        (
            "command_scripts",
            "in_skill",
            "linked_files",
            "markdown_links",
            "module_locator.locate",
            "needs_evidence",
            "nested_flags",
            "outside_lines",
            "outside_scripts",
            "passed_over",
            "python_constants",
            "repository_root",
            "runs_repository_code",
            "scripts_beside",
            "shown_path",
            "skill_folder",
            "skill_scripts",
            "status_lines",
        ),
    ),
    "identity": (
        "keys a path as the file system compares it, its links followed: a skill folder or file locate finds, or a "
        "path the skill's Markdown names, compared as opened_path resolves it",
        ("available_lines", "linked_files", "locate", "outside_lines"),
    ),
    "is_main_name": (
        "asks a folder whether a name it lists is its SKILL.md: agent_skills.is_skill_file reads the folder's own "
        "listing and the entry's own metadata, following no link",
        ("category", "main_files", "tools"),
    ),
    "script_is_present": (
        "whether a script a command names is there, as the runtime running the command finds it, its links followed; "
        "one leading out of the skill folder is a script outside it, which the status lines read by design",
        ("command_scripts", "runs_repository_code"),
    ),
    "listed_entries": (
        "lists a folder outside the examined one: the source tree's runtime prompt samples, its trigger scenarios, or "
        "the agent definitions beside the skill's root",
        ("defined_agents", "listed_in_folders", "sample_lines"),
    ),
    "listed_in_folders": (
        "lists the source tree's trigger scenario folders for the skill, outside the examined folder",
        ("trigger_scenario_lines",),
    ),
    "plugin_roots": (
        "locate's search of the plugin folder the user names, or of Claude Code's plugin cache in the home",
        ("plugin_skill_roots",),
    ),
    "manifest_name": (
        "a plugin's manifest, at the plugin root beside a skill folder or one locate searches",
        ("plugin_of", "plugin_skill_roots"),
    ),
    "available_lines": (
        "locate's listing of each skill root, in the home and the repository, when no skill matches",
        ("locate",),
    ),
    "skill_folders": (
        "lists the skills folder holding the audited skill, whose siblings it may invoke; each sibling's own files "
        "are read through a reader bound to its folder",
        ("invoked_evidence",),
    ),
    "dependency_folders": (
        "whether a skill this one depends on has a folder beside it; that folder's files are read through a reader "
        "bound to it",
        ("setting_lines",),
    ),
    "source_root": (
        "whether the source tree around the skill folder holds the skill's deploy metadata",
        (
            "declared_agents",
            "declared_lines",
            "dependency_folders",
            "plugin_lines",
            "runtime_lines",
            "sample_lines",
            "skill_files",
            "trigger_scenario_lines",
        ),
    ),
    "read_metadata": (
        "a skill's deploy metadata in the source tree around the skill folder",
        ("declared_agents", "declared_lines", "own_needs", "runtime_lines", "skill_closure", "unreadable_metadata"),
    ),
    "unreadable_metadata": (
        "whether a deploy metadata file the source tree holds is there though it cannot be read",
        ("declared_agents", "declared_lines", "own_needs", "runtime_lines"),
    ),
    "plugin_lines": (
        "the other skills the plugin holding a skill folder carries, or the source tree's marketplace and plugin plans",
        ("inventory",),
    ),
    "trigger_scenario_lines": (
        "the source tree's trigger scenarios for the skill, under TRIGGER_SCENARIOS",
        ("tools",),
    ),
    "sample_lines": ("whether the source tree holds a folder of the skill's runtime prompt samples", ("scan",)),
    "repository_root": (
        "the nearest folder above the skill folder with a .git entry, from which its commands run repository tools",
        ("status_lines",),
    ),
    "outside_scripts": (
        "lists the scripts beside one a command runs from the repository root or another skill, or "
        "ejryan89-skill-core's, outside the examined folder, whose status lines the audit compares",
        ("scripts_beside",),
    ),
    "defined_agents": (
        "lists the agent definitions beside the skill's root: agents/, ~/.claude/agents, or a plugin's",
        ("agent_lines", "named_agents", "plugin_lines", "started_agents"),
    ),
    "read_agent": (
        "reads an agent definition beside the skill's root, outside the examined folder",
        ("agent_lines", "started_agents"),
    ),
    "plugin_hooks": (
        "whether the plugin holding the skill folder has a hooks file, beside the skill's root",
        ("agent_lines",),
    ),
    "powershell_script_commands": (
        "hands a script's text, already read, to pwsh through a request file powershell_commands writes and reads "
        "under the private temporary directory: the skill's own file, no path of the examined folder",
        ("script_git_subcommands",),
    ),
    "provenance": ("audit-skill's own provenance file beside this script, or the one a test names", ("main",)),
}


class InputError(ValueError):
    pass


def posix(path: Path) -> str:
    return path.as_posix()


def require_kind(path: Path, what: str, shown: str, *, folder: bool) -> None:
    """Refuse `path` unless a file, or with `folder` a folder, is there: as `<what> not found: <shown>` only when
    nothing is, and as `<what> <shown> is not a file` or `is not a folder` when something else is, never as missing.
    A stat that fails raises its OSError, which main reports as a path it cannot read, as it does for every read."""
    try:
        present = is_directory(path) if folder else is_present(path)
    except (NotADirectory, NotAFile):
        raise InputError(f"{what} {shown} is not a {'folder' if folder else 'file'}") from None
    if not present:
        raise InputError(f"{what} not found: {shown}")


def utf8_text(data: bytes) -> str | None:
    """A file's bytes as UTF-8 text, or None for binary or undecodable content."""
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def outside_bytes(path: Path) -> bytes:
    """The bytes of a file outside the skill folder the audit examines, which it reads by design, as TREE_READ_ALLOWED
    states; a file of the folder is read through its reader."""
    return path.read_bytes()


def file_text(reader: TreeReader, path: Path) -> str | None:
    """The UTF-8 text of a file of the folder `reader` is bound to, or None for binary or undecodable content."""
    return utf8_text(reader.contents(path))


def measured(reader: TreeReader, path: Path, kind: str) -> tuple[int, str | None]:
    """The size and UTF-8 text of a file of the folder `reader` is bound to, of the inventory's category `kind`: a
    helper or data file larger than the reader's bound is sized from its status, through the reader, and counts as no
    text, since it is measured rather than loaded; a main or doc file is read whole, within the bound, or not at all."""
    if kind not in ("main", "doc"):
        size = reader.status(path).st_size
        if size > dotnet_solution.READ_LIMIT_BYTES:
            return size, None
    data = reader.contents(path)
    return len(data), utf8_text(data)


def outside_text(path: Path) -> str | None:
    """The UTF-8 text of a file outside the examined folder (`outside_bytes`), or None for binary or undecodable
    content."""
    return utf8_text(outside_bytes(path))


def os_reason(error: OSError) -> str:
    """Why a read failed: the reader's own reason for a file it does not read, else the operating system's."""
    if isinstance(error, FileRefused):
        return error.reason
    return error.strerror or str(error)


def required_text(path: Path, read: Callable[[Path], bytes]) -> str:
    """The UTF-8 text `read` gives for a file, a reader's `contents` for a file of the folder it is bound to, or
    `outside_bytes` for one outside it; one that cannot be read, such as a directory in its place, one the reader does
    not touch, or one that is binary or not UTF-8 fails rather than reading as empty."""
    try:
        text = utf8_text(read(path))
    except TreeRefused as exc:
        raise InputError(refused_text(exc)) from exc
    except OSError as exc:
        raise InputError(f"cannot read {posix(path)}: {os_reason(exc)}") from exc
    if text is None:
        raise InputError(f"cannot read {posix(path)}: {NOT_UTF8}")
    return text


def require_text(path: Path, reader: TreeReader | None = None) -> str:
    """`required_text` of a file of the folder `reader` is bound to, read through it, or, with no reader, through one
    bound to the folder holding the file."""
    return required_text(path, (reader or TreeReader(path.parent)).contents)


def refused_text(error: TreeRefused) -> str:
    """A path the reader does not touch, as `<path> cannot be examined: <reason>`, the path in forward slashes."""
    return f"{posix(error.path)} cannot be examined: {error.reason}"


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


def runs_repository_code(script: Path, skill: Path | None = None) -> bool:
    """Whether a Python script a command of the skill folder `skill` runs declares, as a module-level non-empty string,
    that it starts repository code: one statement at its top level binds the name, and module_assignments'
    sole_assignment reads it as that name alone, so a chained, unpacking, augmented, or repeated assignment declares
    nothing, which validation refuses. The script is read where the runtime opens it (`named_bytes`), of the skill
    folder holding it when no skill is given."""
    folder = skill if skill is not None else script.parent
    present = not passed_over(folder, script) and script_is_present(script)
    text = utf8_text(named_bytes(folder, opened_path(script))) if present else None
    try:
        tree = python_reader.parse(text or "")
    except python_reader.CONTENT_ERRORS:
        return False
    binding = [node for node in tree.body if REPOSITORY_CODE_DECLARATION in assigned_names(node)]
    if len(binding) != 1 or (assignment := sole_assignment(binding[0])) is None:
        return False
    value = assignment[1]
    return isinstance(value, ast.Constant) and isinstance(value.value, str) and bool(value.value.strip())


def repository_code_commands(skill_dir: Path, commands: list[tuple[int, str]]) -> frozenset[str]:
    """The commands, each part of a joined line on its own, that run a script, through the skill's directory, that
    declares it starts repository code."""
    return frozenset(
        command
        for _, line in commands
        for command in command_parts(line)
        if any(runs_repository_code(skill_dir / script, skill_dir) for script in SKILL_DIR_SCRIPT.findall(command))
    )


def fence_commands(body: list[str], start: int) -> list[tuple[int, str]]:
    """Each command line in a shell or PowerShell fence, wherever it sits, with its file line number: each line of
    the fence, or, in a session, each line session_commands reads, without its prompt, and in a BACKSLASH_FENCES fence
    joined with the lines a backslash continues it into, as continued_commands joins them."""
    commands: list[tuple[int, str]] = []
    for fence in find_fences(body):
        label = fence.language.casefold()
        if label not in SHELL_FENCES | POWERSHELL_FENCES:
            continue
        lines = session_commands(fence, body)
        if lines is None:
            lines = [(index, fence.content(body[index])) for index in fence.body()]
        for index, text in continued_commands(lines) if label in BACKSLASH_FENCES else lines:
            command = COMMAND_JOIN.sub("", text.strip())
            if command and not command.startswith("#"):
                commands.append((start + index + 1, command))
    return commands


def continues(command: str) -> bool:
    """Whether Bash runs a command's line on into the next: it ends in a backslash no other escapes, as
    CONTINUED_LINE reads one, and is no comment's line, whose backslash continues nothing."""
    return CONTINUED_LINE.search(command) is not None and not command.lstrip().startswith("#")


def continued_commands(lines: list[tuple[int, str]]) -> list[tuple[int, str]]:
    r"""Each command of a fence's lines, by its first line's index: a line that continues, as continues reads it,
    runs on into the next, the backslash and the line break dropped as Bash drops them, the next line's blanks
    kept, so `git status \` and `  --short` run `git status   --short`. A comment's line continues nothing, and a line
    that ends the fence keeps its backslash. Only running the command would settle a backslash that ends a quoted
    string's line, which Bash keeps inside single quotes, or a comment that follows a command, as in `echo a # note \`;
    both join the next line here."""
    commands: list[tuple[int, str]] = []
    continued: tuple[int, str] | None = None
    for index, text in lines:
        if continued is not None:
            index, text = continued[0], continued[1][:-1] + text
        if continues(text):
            continued = (index, text)
        else:
            continued = None
            commands.append((index, text))
    return commands if continued is None else [*commands, continued]


def session_commands(fence: Fence, lines: list[str]) -> list[tuple[int, str]] | None:
    r"""The body lines a SESSION_FENCES fence runs, by index, each as CommonMark gives it to the fence's content
    without its prompt, or None for a fence of another label or one with no line SESSION_PROMPT starts, which is no
    session: no prompt tells a command from output there, and a fence labelled so may hold commands alone, which a
    model runs. A session runs the lines a prompt starts and the lines that continue them: a line after one that
    continues, as continues reads it, with or without the `> ` prompt Bash shows there, as Pygments' console lexer
    reads it, though Pygments also continues a comment's line, as `$ # note \`, which Bash does not; and a `> ` line
    right after one, which Bash prompts for a command that is not yet complete, where Pygments reads it as output.
    Any other line is output.

    SESSION_PROMPT reads the prompt forms Pygments reads, with these differences. Pygments takes a bare `# ` as
    root's prompt: here it starts a comment, so a root session's commands written after one read as comments and its
    output as commands. Pygments also takes a prompt with no space after it, as in `$HOME/bin/tool`, a word starting
    with `sh` before one, as in `shellcheck # note`, and a prompt character inside a user and host or the word after
    them, as in `user@host:~$ ls # list`, whose command it reads as `list`; here the first two are not prompts and the
    third's command is `ls # list`. Pygments reads a prompt after two virtual environments, as `(venv) (base) $ `,
    after a bracket and a further word, as `[user@host dir] (main)$ `, or followed by a tab, which here is not one; and
    here a user and host may start with any word character, `.`, or `-`, so `-x:y$ ` is a prompt, where Pygments takes
    a word character first. A command line that starts like a bracketed prompt, as `[ -d build ] # check`, makes a
    fence of bare commands a session and its other lines output. A line of output that starts like a prompt, such as
    curl's `  % Total` progress header, reads as a command. So does a `> ` line of output right after a command line,
    as diff prints one, a line after a backslash that ends a quoted string's line or a comment, as in
    `$ echo a # note \`, and a here-document's body, which Bash also prompts for with `> `; only running the session
    would settle them."""
    if fence.language.casefold() not in SESSION_FENCES:
        return None
    return prompted_commands(fence, lines, SESSION_PROMPT)


def prompted_commands(fence: Fence, lines: list[str], prompt_pattern: re.Pattern[str]) -> list[tuple[int, str]] | None:
    """The body lines a fence runs as a session, by index, each as CommonMark gives it to the fence's content without
    its prompt, where `prompt_pattern` reads a command line's prompt, as session_commands reads them, or None for a
    fence no line of which that prompt starts."""
    contents = [(index, fence.content(lines[index])) for index in fence.body()]
    if not any(prompt_pattern.match(text) for _, text in contents):
        return None
    commands: list[tuple[int, str]] = []
    prompted = continued = False
    for index, text in contents:
        if (prompt := prompt_pattern.match(text)) is not None:
            command = text[prompt.end() :]
        elif continued or (prompted and CONTINUATION_PROMPT.match(text)):
            command = CONTINUATION_PROMPT.sub("", text, count=1)
        else:
            prompted = continued = False
            continue
        prompted, continued = True, continues(command)
        commands.append((index, command))
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
        return len(words) > 1 and runtime_support.SUBCOMMAND_NAME.fullmatch(words[1]) is not None
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


def leads_out(reader: TreeReader, path: Path) -> bool:
    """Whether a name a folder of the reader's tree lists is a link the reader does not follow, one that leads out of
    the tree: it is passed over as a link to a folder is, never followed, so it is not a file of the folder. A name that
    is not there is no link."""
    try:
        linked = reader.is_link(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    return linked and reader.refusal(path) is not None


def is_listed_file(reader: TreeReader, path: Path) -> bool:
    """Whether a regular file of the folder `reader` is bound to is at a path a listing found or the skill names,
    following the links that stay inside it: False when nothing or something else is there, or a link that leads out of
    the folder (`leads_out`), and the stat's OSError when it cannot tell, so a file the audit cannot look at fails the
    command rather than being left out of what it measures."""
    if leads_out(reader, path):
        return False
    try:
        return reader.present(path)
    except NotAFile:
        return False


def is_outside_file(path: Path) -> bool:
    """`is_listed_file` of a place outside the examined folder, which the audit reads by design, as TREE_READ_ALLOWED
    states, following its links."""
    try:
        return is_present(path)
    except NotAFile:
        return False


def is_main_name(path: Path) -> bool:
    """Whether a name a folder lists is the folder's SKILL.md, as `agent_skills.is_skill_file` asks the folder's own
    listing, following no link."""
    return is_skill_file(path)


def opened_path(path: Path) -> Path:
    """`path` as the runtime opens it, every link and `..` followed (`path_containment.resolved_path`): the examined
    folder's own place, a file of it the reader let through, or a path its Markdown names, one leading out of the
    folder being an outside read by design."""
    return resolved_path(path)


def listed_entries(folder: Path, pattern: str) -> list[Path]:
    """The entries of `folder` whose names match the one-segment glob `pattern`, in path order, as Path.glob matches
    them, but listed with `file_presence.listed`, which raises for a folder it cannot list, where glob matches nothing
    there and the audit would answer as though the folder were empty, and leaves out the AppleDouble sidecars macOS
    writes beside a file on a FAT, exFAT, or SMB volume."""
    return [path for path in listed(folder) if fnmatch.fnmatch(path.name, pattern)]


def listed_in_folders(folder: Path, name: str) -> list[Path]:
    """The entries named `name` in the folders `folder` holds, in path order, as Path.glob("*/<name>") matches them;
    `folder` or a folder in it that cannot be listed, or looked at, raises."""
    return sorted(
        path
        for child in folder.iterdir()
        if is_directory(child, other_is_absent=True)
        for path in listed_entries(child, name)
    )


def main_files(directory: Path) -> list[Path]:
    """A skill folder's SKILL.md, the entry its listing gives under that name, through a reader bound to the folder, so
    a SKILL.md that is a link leading out of it is none."""
    reader = TreeReader(directory)
    if not reader.folder(directory):
        return []
    return sorted(path for path in reader.listing(directory) if is_main_name(path) and is_listed_file(reader, path))


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
    if scope == "source" and source_root(directory) is None:
        return []
    return main_files(directory)


def identity(path: Path) -> str:
    return path_key(path, resolve=True)


def plugin_roots(folder: Path, depth: int = PLUGIN_DEPTH) -> list[Path]:
    """Each plugin root at or below folder, down to depth folders, never searching inside a plugin."""
    if is_present(folder / skill_menu.PLUGIN_MANIFEST):
        return [folder]
    if depth == 0 or not is_directory(folder):
        return []
    children = sorted(
        (child for child in folder.iterdir() if is_directory(child, other_is_absent=True)),
        key=lambda path: path.name.casefold(),
    )
    return [root for child in children for root in plugin_roots(child, depth - 1)]


def manifest_name(root: Path) -> str | None:
    """A plugin's name, from its manifest, or None when the manifest names none."""
    try:
        document = json_reader.read_file(root / skill_menu.PLUGIN_MANIFEST)
    # ValueError covers a decode error, UnicodeError, an over-long integer, and nesting json_reader refuses
    except (OSError, ValueError):
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
        require_kind(folder, "plugin folder", posix(folder), folder=True)
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
            if not (scope == "project-agents" and pointer in (outside_text(path) or ""))
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
        if not is_directory(root):
            continue
        for directory in sorted(root.iterdir(), key=lambda path: path.name.casefold()):
            if (
                is_directory(directory, other_is_absent=True)
                and skill_files(scope, directory)
                and identity(directory) not in listed
            ):
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
    """The root of the repository containing directory, or None when git answers that it lies in no repository.

    Raises InputError naming git's reason when git cannot run, finish, or read the checkout, so a repository git
    cannot read is never searched as no repository at all.
    """
    try:
        return (git or GitClient()).rev_parse_path("--show-toplevel", directory=directory)
    except GitError as exc:
        raise InputError(f"git could not read the checkout at {directory.as_posix()}: {exc}") from exc


# --- inventory ------------------------------------------------------------------------------------


def category(relative: Path, directory: Path) -> str:
    """What a file of the skill folder `directory` is, by its path relative to the folder."""
    if len(relative.parts) == 1 and is_main_name(directory / relative):
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
        lines = text_lines(text, keepends=True)
        languages, _ = parse_fences([line.rstrip("\r\n") for line in lines])
        code = sum(len(line) for line, language in zip(lines, languages, strict=True) if language is not None)
        prose = len(text) - code
    else:
        code, prose = len(text), 0
    return -(-(3 * prose + 4 * code) // 12)


def normalized_blocks(text: str) -> list[tuple[int, tuple[str, ...]]]:
    _, blocks = parse_fences(text_lines(text))
    result = []
    for block in blocks:
        body = tuple(line.strip() for line in block.body if line.strip())
        result.append((block.line, body))
    return result


def folder_files(directory: Path) -> list[Path]:
    """Every file of a skill folder that inventory sizes, in the order it lists them, as `folder_walk` walks the folder:
    its one skip set, compared case-insensitively, and no nested checkout or link to a folder entered. A skipped folder
    is never listed, so a folder it cannot list inside one is never met; one elsewhere, or an entry the walk cannot
    examine, raises its own error, so no file the walk reaches is left out of the measure unreported. A Markdown file
    the skill's own Markdown links in the folder, which the runtime loads wherever it lies, is listed too, even where
    the walk passes over its folder, so a linked file is never left out of the measure either. The folder is read
    through a reader bound to it, so a file it holds through a link that leads out of it is passed over, as a link to a
    folder is, and never followed."""
    reader = TreeReader(directory)
    walk = reader.search(directory, lambda _name: True)
    for entry in walk.not_entered:
        if entry.error is not None:
            raise entry.error
    files = [path for path in walk.files if is_listed_file(reader, path)]
    return sorted(
        [*files, *linked_files(directory, files)],
        key=lambda path: posix(path.relative_to(directory)).casefold(),
    )


def markdown_links(directory: Path, relative: str, kind: str, text: str) -> list[tuple[int, str, Path]]:
    """Each Markdown path a main or doc file of the skill folder names, out of the frontmatter, in the order written:
    the line number, the reference as written, and the file it leads to with every `..` and link followed, as the
    runtime reading it would. A link's destination is read as CommonMark reads it, through `link_destinations`:
    percent-decoded, between `<` and `>` with spaces, absolute, or with a backslash, read as the platform reads a
    path: a separator on Windows, part of the name elsewhere; and a path named in prose or code, outside every link's
    destination, through PROSE_REFERENCE, whole and as written, its backslash read as a link's is. A path resolves
    from the naming file, or from the skill folder after ${CLAUDE_SKILL_DIR}/, and an absolute one from the root it
    names. Every check of the files a skill's Markdown names reads them here."""
    lines = text_lines(text)
    start = body_start(lines) if kind == "main" else 0
    naming = (directory / relative).parent
    # (line index, column, reference as written, path as the runtime reads it)
    named: list[tuple[int, int, str, str]] = []
    taken: dict[int, list[tuple[int, int]]] = {}
    for destination in link_destinations(lines[start:]):
        index = start + destination.line - 1
        taken.setdefault(index, []).append((destination.start, destination.end))
        path = destination.path
        if not destination.is_url and Path(path).suffix.casefold() == ".md":
            named.append((index, destination.start, destination.written, path))
    commands = fence_reads(lines, start)
    for index in range(start, len(lines)):
        links = taken.get(index, [])
        reads = commands.get(index)
        named.extend(
            (index, match.start(), match.group(0), match.group(0))
            for match in PROSE_REFERENCE.finditer(lines[index])
            if not any(left < match.end() and match.start() < right for left, right in links)
            and (reads is None or taken_from(reads, match.group(0)))
        )
    found: list[tuple[int, str, Path]] = []
    for index, _, reference, path in sorted(named, key=lambda name: name[:2]):
        base = naming
        if path.startswith(SKILL_DIR_PREFIX):
            base, path = directory, path[len(SKILL_DIR_PREFIX) :]
        found.append((index + 1, reference, opened_path(base / path)))
    return found


def taken_from(reads: Counter[str], path: str) -> bool:
    """Whether `reads` holds one more read of `path`, which this takes from it."""
    if reads[path] <= 0:
        return False
    reads[path] -= 1
    return True


def fence_reads(lines: list[str], start: int) -> dict[int, Counter[str]]:
    """The Markdown paths a file command reads on each body line of a shell or PowerShell fence below the frontmatter,
    by the line's index, each as PROSE_REFERENCE reads it: on a line no file command reads a path on, none. A file
    command is one of SHELL_FILE_COMMANDS, which prints what it reads for the model, and the path is one it reads. A
    fence BASH_FENCES labels is read whole through the one Bash grammar, each line as CommonMark gives it to the
    fence's content, and its paths are those in the words, and the `<` redirections' targets, of each simple command
    that runs a file command, wherever Bash starts one and through each wrapper, eval, or shell's -c that runs it, as
    bash_programs reads it. Any other such fence, and a Bash fence the grammar refuses, or a here-document in it runs
    to its end of, or in which it finds a command a prompt starts, as runs_a_prompt reads one, is read by command
    line, as command_line_paths reads each: such a Bash fence a line of which BASH_PROMPT starts as a session whose
    commands prompted_commands reads, and any other by first_word_commands. A line outside such fences is not here:
    every path on it is read."""
    reads: dict[int, Counter[str]] = {}
    for fence in find_fences(lines):
        label = fence.language.casefold()
        if fence.opener < start or label not in BASH_FENCES | FIRST_WORD_FENCES:
            continue
        body = list(fence.body())
        reads.update((index, Counter()) for index in body)
        session = None
        if label in BASH_FENCES:
            found = grammar_paths("\n".join(fence.content(lines[index]) for index in body))
            if found is not None:
                for line, path in found:
                    reads[body[line - 1]][path] += 1
                continue
            session = prompted_commands(fence, lines, BASH_PROMPT)
        any_case = label in ANY_CASE_FENCES
        for index, command in first_word_commands(fence, lines) if session is None else session:
            reads[index] = command_line_paths(command, any_case=any_case)
    return reads


def grammar_paths(text: str) -> list[tuple[int, str]] | None:
    """The paths file_command_paths reads in a Bash fence's text, or None where the grammar does not read it as a
    script: it refuses the text, a here-document in it runs to its end, or a command in it is one a prompt starts."""
    try:
        if open_here_document(text) or any(runs_a_prompt(command) for command in bash_syntax.simple_commands(text)):
            return None
        return file_command_paths(text)
    except BashSyntaxError:
        return None


def runs_a_prompt(command: Simple) -> bool:
    """Whether a simple command's program, past the assignments before it, is an unquoted `$` alone: a prompt written
    before a command, as in `$ cat a.md`, which the grammar reads as the program. A here-document's body and a quoted
    string are no command's words, so a `$ ` line inside one never makes the fence a session."""
    words = list(dropwhile(lambda word: word.assignment is not None, command.words))
    return bool(words) and words[0].literal == "$"


def command_line_paths(command: str, *, any_case: bool) -> Counter[str]:
    """The Markdown paths a command line read by its first word gives a file command to read, as PROSE_REFERENCE reads
    them: none unless first_word_runs_file_command reads its first word as one, and then each from that word to the
    line's first COMMAND_END, after which a path is a redirection's target, as in `> log.md`, or another command's, as
    in `| Out-File w.md`."""
    if not first_word_runs_file_command(command, any_case=any_case):
        return Counter()
    words = COMMAND_END.split(command, maxsplit=1)[0]
    return Counter(match.group(0) for match in PROSE_REFERENCE.finditer(words))


def file_command_paths(text: str) -> list[tuple[int, str]]:
    """Each Markdown path a Bash text gives a file command, one of SHELL_FILE_COMMANDS, to read, as PROSE_REFERENCE
    reads it in the source of a word, or of a `<` redirection's target, of a simple command that runs one, as
    bash_programs reads the programs a command runs; with the line, counted from 1, the path stands on. A path in
    another redirection's target, such as `> out.md`, or in a command that runs no file command, such as
    `gh pr create --body-file b.md`, is one the command writes or hands its program at run time. Raises
    BashSyntaxError where the grammar refuses the text, or a script eval or a shell's -c runs in it."""
    found: list[tuple[int, str]] = []
    for command in bash_syntax.simple_commands(text):
        programs = simple_command_programs(command)
        if not any(command_wrappers.bash_program(words[0]) in SHELL_FILE_COMMANDS for _, words in programs):
            continue
        inputs = [redirect.target for redirect in command.redirects if redirect.operator == "<" and redirect.target]
        for word in [*command.words, *inputs]:
            line, position = word.line, 0
            for match in PROSE_REFERENCE.finditer(word.raw):
                line += word.raw.count("\n", position, match.start())
                position = match.start()
                found.append((line, match.group(0)))
    return found


def linked_files(directory: Path, files: list[Path]) -> list[Path]:
    """Each Markdown file in the skill folder that the main or a doc file among `files`, or one found so, links, and
    `files` does not hold, as the walk passed over a folder it skips or a nested checkout: by its path in the folder,
    read through a reader bound to the folder."""
    reader = TreeReader(directory)
    root = opened_path(directory)
    known = {identity(path) for path in files}
    pending = list(files)
    found: list[Path] = []
    while pending:
        source = pending.pop(0)
        relative = posix(source.relative_to(directory))
        kind = category(Path(relative), directory)
        text = file_text(reader, source) if kind in ("main", "doc") else None
        if text is None:
            continue
        for _, _, target in markdown_links(directory, relative, kind, text):
            if not within(target, root) or identity(target) in known:
                continue
            path = directory.joinpath(*target.parts[len(root.parts) :])
            if not is_listed_file(reader, path):
                continue
            known.add(identity(target))
            found.append(path)
            pending.append(path)
    return found


def inventory(directory: Path) -> list[str]:
    require_kind(directory, "skill directory", str(directory), folder=True)
    reader = TreeReader(directory)
    files = folder_files(directory)
    lines: list[str] = []
    totals = {name: [0, 0, 0] for name in CATEGORIES}
    flags: list[str] = []
    texts: dict[str, tuple[str, str]] = {}
    unreadable: list[str] = []
    has_main = False
    for path in files:
        relative = path.relative_to(directory)
        kind = category(relative, directory)
        has_main = has_main or kind == "main"
        size, text = measured(reader, path, kind)
        if text is None and kind in ("main", "doc"):
            # The runtime still loads it, so it has no measured cost: no FILE line, and nothing in TOTAL.
            unreadable.append(f"UNREADABLE {posix(relative)} {NOT_UTF8}")
            continue
        tokens = estimate_tokens(text, kind)
        lines.append(f"FILE {kind} {size} {tokens} {posix(relative)}")
        for index, value in enumerate((1, size, tokens)):
            totals[kind][index] += value
        if text is not None:
            texts[posix(relative)] = (kind, text)
            flags.extend(structure_flags(kind, text, posix(relative)))
    lines.extend(f"TOTAL {kind} {totals[kind][0]} {totals[kind][1]} {totals[kind][2]}" for kind in CATEGORIES)
    lines.append("TOTAL all " + " ".join(str(sum(totals[kind][i] for kind in CATEGORIES)) for i in range(3)))
    if not has_main:
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
    lines = text_lines(text)
    if kind == "main":
        body = len(lines) - body_start(lines)
        return [f"BODY_OVER_500_LINES {body}"] if body > BODY_LINE_LIMIT else []
    if kind != "doc" or len(lines) <= DOC_TOC_LINE_LIMIT:
        return []
    languages, _ = parse_fences(lines)
    for line, language in zip(lines, languages, strict=True):
        heading = ATX_HEADING.match(line) if language is None else None
        if heading and len(heading.group("marks")) >= 2:
            if CONTENTS_HEADING.fullmatch(heading.group("text")):
                return []
            break
    return [f"DOC_NO_TOC {len(lines)} {relative}"]


def nested_flags(directory: Path, texts: dict[str, tuple[str, str]]) -> list[str]:
    """Reference files reached only through another reference file; published guidance links each from SKILL.md. Links
    are read through `markdown_links`, as the measure reads them."""
    docs = {opened_path(directory / relative) for relative, (kind, _) in texts.items() if kind == "doc"}
    linked: set[Path] = set()
    for relative, (kind, text) in texts.items():
        if kind == "main":
            linked.update(target for _, _, target in markdown_links(directory, relative, kind, text))
    flags: list[str] = []
    for relative, (kind, text) in texts.items():
        if kind != "doc":
            continue
        source = opened_path(directory / relative)
        for number, reference, target in markdown_links(directory, relative, kind, text):
            if target != source and target in docs and target not in linked:
                flags.append(f"NESTED_REFERENCE {relative}:{number} {reference}")
    return flags


def outside_lines(directory: Path, texts: dict[str, tuple[str, str]]) -> list[str]:
    """Markdown the skill's own Markdown points to outside its folder: a read, and its cost, on every run. A path leads
    out by a leading `..`, a later one, or a link, to a folder or a file, that leads out of the folder, which the
    inventory passes over rather than sizing as a file of the folder."""
    root = opened_path(directory)
    lines: list[str] = []
    counted: dict[str, tuple[int, int]] = {}
    unreadable: dict[str, str] = {}
    for relative, (kind, text) in texts.items():
        if kind not in ("main", "doc"):
            continue
        for number, reference, target in markdown_links(directory, relative, kind, text):
            key = identity(target)
            if within(target, root) or key in unreadable:
                continue
            where = f"{relative}:{number}"
            try:
                present = is_outside_file(target)
            except OSError as exc:
                unreadable[key] = f"UNREADABLE {outside_path(root, target)} {exc.strerror or exc}"
                continue
            if not present:
                lines.append(f"OUTSIDE_MISSING {where} {reference}")
                continue
            if key not in counted:
                data = outside_bytes(target)
                outside = utf8_text(data)
                if outside is None:
                    unreadable[key] = f"UNREADABLE {outside_path(root, target)} {NOT_UTF8}"
                    continue
                counted[key] = (len(data), estimate_tokens(outside, "doc"))
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
    return root if is_present(metadata_path(root, directory.name)) else None


# The metadata keys that hold a list of names, which the inventory reads.
NAME_LISTS = ("shared_deps", "skill_deps", "agent_deps", "tools", "optional_tools")


def read_metadata(root: Path, name: str) -> dict[str, object] | None:
    """A skill's deploy metadata in its source tree, or None when the file is missing, is not a JSON object, or holds
    a name list of another kind than a list of names, whose names cannot then be known. The file is read by
    skill_menu's read_source_file and a name list by its name_list, as the deployer's loader reads them, so a
    byte-order mark is passed over, and a null name list is none, as an absent one is."""
    try:
        metadata = skill_menu.read_source_file(metadata_path(root, name))
    # ValueError covers a decode error, UnicodeError, an over-long integer, and nesting json_reader refuses
    except (OSError, ValueError):
        return None
    if not isinstance(metadata, dict):
        return None
    if any(skill_menu.name_list(metadata.get(key)) is None for key in NAME_LISTS):
        return None
    return metadata


def names(metadata: dict[str, object] | None, key: str) -> list[str]:
    """The names a metadata list holds, which read_metadata has checked, or none for a key that is absent."""
    value = (metadata or {}).get(key)
    return [name for name in value if isinstance(name, str)] if isinstance(value, list) else []


def metadata_path(root: Path, name: str) -> Path:
    return root / "deploy-meta" / f"{name}.json"


def unreadable_metadata(root: Path, name: str) -> bool:
    """Whether the skill has a metadata file read_metadata cannot read, whose names are then unknown rather than none,
    as opposed to no metadata file at all, a link to nothing included, which a stat that fails does not show."""
    if read_metadata(root, name) is not None:
        return False
    try:
        return is_there(metadata_path(root, name))
    except OSError:
        return True


def skill_closure(root: Path, name: str) -> list[str]:
    """The skill and every skill its metadata's skill_deps reach, in the order the deployer's selection reaches them,
    through skill_menu's one walk of skill dependencies."""
    return skill_menu.closure([name], lambda current: names(read_metadata(root, current), "skill_deps"))


def declared_agents(directory: Path) -> dict[str, str] | None:
    """The agents a deployment of the skill installs, each with the skill that declares it in agent_deps, its own
    first: None outside a source tree, where no metadata says, and when a skill the closure reaches has metadata that
    cannot be read, which may declare any agent or dependency."""
    root = source_root(directory)
    if root is None:
        return None
    closure = skill_closure(root, directory.name)
    if any(unreadable_metadata(root, skill) for skill in closure):
        return None
    agents: dict[str, str] = {}
    for skill in closure:
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
        return [f"DECLARED_UNREADABLE {posix(metadata_path(root, directory.name))}"]
    agents = declared_agents(directory) or {}
    return [
        *(
            f"DECLARED_UNREADABLE {posix(metadata_path(root, skill))}"
            for skill in skill_closure(root, directory.name)[1:]
            if unreadable_metadata(root, skill)
        ),
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
            if is_directory(child, other_is_absent=True) and child.name != directory.name and main_files(child)
        ]
        return [
            f"CARRIED_BY {name}",
            *(f"PLUGIN_CARRIES skill {skill}" for skill in skills),
            *(f"PLUGIN_CARRIES agent {agent}" for agent in defined_agents(directory)),
        ]
    root = source_root(directory)
    if root is None:
        return []
    marketplace = root / skill_menu.PLUGIN_MARKETPLACE
    try:
        if not is_present(marketplace):
            return []
    except OSError as exc:
        return [f"PLUGINS_UNREADABLE {posix(marketplace)} cannot be read: {exc.strerror or exc}"]
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
    for spec_file in listed_in_folders(scenarios, "scenario.json") if is_directory(scenarios) else []:
        try:
            spec = json_reader.read_file(spec_file)
        except OSError as exc:
            output.append(f"TRIGGER_SCENARIO_UNREADABLE {posix(spec_file)} {exc.strerror or exc}")
            continue
        # ValueError covers a decode error, UnicodeError, an over-long integer, and nesting json_reader refuses
        except ValueError as exc:
            output.append(f"TRIGGER_SCENARIO_UNREADABLE {posix(spec_file)} not JSON: {exc}")
            continue
        if not isinstance(spec, dict):
            output.append(f"TRIGGER_SCENARIO_UNREADABLE {posix(spec_file)} not a JSON object")
        elif spec.get("mode") == "trigger":
            lists = {key: spec.get(key, []) for key in ("requests", "near_misses")}
            other = [key for key, value in lists.items() if not isinstance(value, list)]
            if other:
                output.append(f"TRIGGER_SCENARIO_UNREADABLE {posix(spec_file)} its {other[0]} is not a list")
                continue
            output.append(f"TRIGGER_SCENARIO {posix(spec_file)} {len(lists['requests'])} {len(lists['near_misses'])}")
    return output or [f"NO_TRIGGER_SCENARIO {posix(scenarios)}"]


def duplicate_flags(texts: dict[str, tuple[str, str]]) -> list[str]:
    """Fenced blocks repeated in Markdown, and helper scripts copied into Markdown fences."""
    flags: list[str] = []
    first: dict[tuple[str, ...], str] = {}
    helpers = {
        path: "\n" + "\n".join(line.strip() for line in text_lines(text) if line.strip()) + "\n"
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
    spans = [] if language is not None else [span.text.strip().strip("\"'") for span in code_spans(line)]
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
    for span in code_spans(line):  # a dot in a code span ends no clause
        masked = blanked(masked, span.text_start, span.end - span.run)
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
    require_kind(path, "skill file", str(path), folder=False)
    lines = text_lines(require_text(path, TreeReader(path.parent)))
    try:
        found = frontmatter.split(lines)
        document = frontmatter.Frontmatter(found[0] if found else [])
        model = document.string("model")
        allowed = parse_allowed_tools(document.value("allowed-tools"))
        entries = allowed_entries(document.value("allowed-tools"))
        listed = listing(document)
        invoked = invocation(document)
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
    output = [f"MODEL {model or 'none'}"]
    if is_main_name(path):
        output += setting_lines(path.parent)
    output += [*listed, *trigger_scenario_lines(path.parent, invoked)]
    output += runtime_lines(path.parent, allowed or [], invoked != "model")
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


def invocation(document: frontmatter.Frontmatter) -> str:
    """Who may start the skill: model, user-only (disable-model-invocation: true), or hidden (also
    user-invocable: false). The INVOCATION line prints it, and a caller takes it from here, never by splitting that
    line back into fields."""
    if not document.user_only():
        return "model"
    return "hidden" if (document.string("user-invocable") or "").strip().casefold() == "false" else "user-only"


def listing(document: frontmatter.Frontmatter) -> list[str]:
    """What every session loads for this skill: its description, unless model invocation is disabled."""
    text = (document.string("description") or "").strip()
    model_invocable = not document.user_only()
    lines = [f"DESCRIPTION {len(text)} {-(-len(text) // 4)}", f"INVOCATION {invocation(document)}"]
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


def inline_code(line: str, code_line: bool = True) -> bool:
    """Whether a line runs an interpreter on code written in the command rather than a script file: Python told -c or
    - (the program read from a pipe or a here-document), Node told -e or -p, or PowerShell told -Command or
    -EncodedCommand, or Windows PowerShell given a command as its first word that is no parameter, each read by the
    options its program takes before the code. On a prose line, `code_line` False, that first word is not read, since
    prose names Windows PowerShell before ordinary words; the scan reads each code span of the line again as code."""
    for match in INTERPRETER.finditer(line):
        words: list[str] = []
        for word in INTERPRETER_WORD.findall(line, match.end()):
            if word in ("|", ";", "&", "`", ")"):
                break
            words.append(word.strip("\"'"))
        program = match.group(1).casefold()
        if program == "node":
            found = node_inline_code(words)
        elif program in POWERSHELL_PROGRAMS:
            found = powershell_inline_code(words, positional_command=code_line and program == "powershell")
        else:
            found = python_inline_code(list(dropwhile(LAUNCHER_VERSION.fullmatch, words)) if program == "py" else words)
        if found:
            return True
    return False


def generated_code(line: str, code_line: bool) -> bool:
    """Whether a line runs an interpreter on code it holds (inline_code): a code line read whole, a prose line read
    whole and then each of its code spans read as code."""
    return inline_code(line, code_line) or (not code_line and any(inline_code(span.text) for span in code_spans(line)))


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


def powershell_inline_code(words: list[str], positional_command: bool = False) -> bool:
    """Whether PowerShell's parameters run a command they hold, as ejryan89-skill-core's powershell_startup reads them:
    -Command, -CommandWithArgs, or -EncodedCommand before any -File, or, given `positional_command`, Windows
    PowerShell's first word that is neither a parameter nor a parameter's value, which it runs as -Command's."""
    return isinstance(startup_command(words, positional_command=positional_command), StartupCommand)


def decomposes_a_value(line: str) -> bool:
    """Whether the line shows a printed value with a space and then names two of its parts, by DECOMPOSED_VALUE."""
    for match in DECOMPOSED_VALUE.finditer(line):
        if sum(part in match.group(1) for part in VALUE_PART.findall(match.group(2))) >= 2:
            return True
    return False


def given_path(path: Path) -> Path:
    """A path the user names, its part up to its last `..` resolved as the system opens it
    (`path_containment.resolved_path`), the user's own choice of where to look, and the names after it kept as named,
    before any reader is bound to it: a reader judges each path below its tree as written, so a folder named through a
    `..` would hold no path it lets through, and a name after the last `..`, such as a SKILL.md that is a link leading
    out of its folder, is then judged by the reader as it would be named any other way. A path with no `..` is kept as
    named, so the lines it prints name it as the user did."""
    if ".." not in path.parts:
        return path
    last = max(index for index, part in enumerate(path.parts) if part == "..")
    return resolved_path(Path(*path.parts[: last + 1])).joinpath(*path.parts[last + 1 :])


def is_given_folder(path: Path) -> bool:
    """Whether a path the user names to scan is a folder, which a reader is then bound to; a folder whose stat fails
    raises, never read as a file to scan."""
    return is_directory(path, other_is_absent=True)


def scanned_files(path: Path) -> list[Path]:
    """A file as given, or a skill folder's main file followed by every doc file inventory lists."""
    if not is_given_folder(path):
        return [path]
    files = folder_files(path)
    main = [file for file in files if category(file.relative_to(path), path) == "main"]
    if not main:
        raise InputError(f"no SKILL.md in {posix(path)}")
    return [*main, *(file for file in files if category(file.relative_to(path), path) == "doc")]


def not_a_file(path: Path) -> bool:
    """Whether something other than a file is at a path the user names to scan: not nothing, and not a link to nothing,
    which file_presence reads as nothing there. A stat that fails raises."""
    try:
        is_present(path)
    except NotAFile:
        return True
    return False


def is_given_file(path: Path) -> bool:
    """Whether a file is at a path the user names to scan, following its links, the user's own choice of what to read;
    `not_a_file` has refused anything else there."""
    return is_present(path)


def scanned_texts(given: list[Path]) -> list[tuple[Path, str]]:
    """Each file scan reads, with its text: a folder's files, as `scanned_files` lists them, through a reader bound to
    the folder, and a file the user names, read as named."""
    folders = {path: TreeReader(path) for path in given if is_given_folder(path)}
    named = [path for path in given if path not in folders]
    # Something other than a file is named so, never as not found, a link to nothing is not found, as file_presence
    # reads it, and a stat that fails raises.
    if wrong := [posix(path) for path in named if not_a_file(path)]:
        raise InputError("not a file: " + ", ".join(wrong))
    if missing := [posix(path) for path in named if not is_given_file(path)]:
        raise InputError("file not found: " + ", ".join(missing))
    return [
        (file, require_text(file, folders[path]) if path in folders else required_text(file, outside_bytes))
        for path in given
        for file in scanned_files(path)
    ]


def scan(given: list[Path]) -> list[str]:
    scanned = scanned_texts(given)
    paths = [path for path, _ in scanned]
    texts = [text for _, text in scanned]
    output: list[str] = []
    runtime_prompts: list[tuple[Path, int]] = []
    for path, text in zip(paths, texts, strict=True):
        lines = text_lines(text)
        if path.suffix.casefold() == ".md":
            start = body_start(lines)
            languages, _ = parse_fences(lines)
        else:
            start, languages = 0, [path.suffix.casefold().lstrip(".") or "text"] * len(lines)
        file_commands, unread = shell_file_commands(path, text, lines, start)
        for index in range(start, len(lines)):
            line, language = lines[index], languages[index]
            if index in unread:
                output.append(f"BASH_UNREADABLE {posix(path)} {index + 1} {unread[index]}")
            text = line.strip()
            if not text:
                continue
            cues = [
                name
                for name, pattern in CUES
                if pattern.search(line) or (name == "generated-code" and generated_code(line, language is not None))
            ]
            cues += [
                name
                for name, pattern in PROSE_CUES
                if pattern.search(line) or (name == "parsed-output" and decomposes_a_value(line))
            ]
            if language is not None:
                cues = [cue for cue in cues if cue in CODE_CUES]
            shell = (
                index in file_commands
                if language is not None
                else any(SPAN_SHELL_COMMAND.match(span.text.strip()) for span in code_spans(line))
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
            found, prompts = delegation_lines(path, lines, languages, start)
            output.extend(found)
            runtime_prompts += [(path, line) for line in prompts]
    return (
        output
        + [line for path in given if is_given_folder(path) for line in agent_lines(path)]
        + sample_lines(runtime_prompts)
        + [line for path in given if is_given_folder(path) for line in status_lines(path)]
    )


def shell_file_commands(path: Path, text: str, lines: list[str], start: int) -> tuple[set[int], dict[int, str]]:
    """The indices of the lines on which a shell command of the file that runs one of SHELL_FILE_COMMANDS starts, and
    the reason for each Bash fence it cannot read, by the index of the fence's opening line: the grammar refuses it,
    or a here-document in it is still open where the fence ends. A Markdown file's Bash is each fence below its
    frontmatter that BASH_FENCES labels, read whole, as a model running it reads it, each line as CommonMark gives it
    to the fence's content; an untagged fence is read the same way when it is Bash the grammar reads, and holds no
    Bash otherwise, as it may hold none. A fence FIRST_WORD_FENCES labels runs the program the first word of each line
    first_word_file_command_lines reads names. A Bash script is its whole text, on the lines `lines` splits it into.
    Any other file, and a fence of any other language, holds no shell command."""
    if path.suffix.casefold() != ".md":
        if path.suffix.casefold() not in BASH_SUFFIXES:
            return set(), {}
        try:
            numbers = file_command_lines(text)
        except BashSyntaxError as exc:
            return set(), {0: str(exc)} if lines else {}
        # Bash counts a script's lines at LF alone, and scan places a line where text_lines breaks it: the line of the
        # place Bash starts a command's line on, read over both line starts once.
        starts, lines_at = line_starts(text, LINE_FEED), line_starts(text)
        return {line_index(lines_at, starts[number - 1]) for number in numbers}, {}
    found: set[int] = set()
    unread: dict[int, str] = {}
    for fence in find_fences(lines):
        untagged = fence.language == ""
        if fence.opener >= start and fence.language.casefold() in FIRST_WORD_FENCES:
            found.update(first_word_file_command_lines(fence, lines))
        if fence.opener < start or not (untagged or fence.language.casefold() in BASH_FENCES):
            continue
        body = list(fence.body())
        content = "\n".join(fence.content(lines[index]) for index in body)
        try:
            numbers = file_command_lines(content)
            if open_here_document(content):
                raise BashSyntaxError("a here-document is still open where the fence ends")
        except BashSyntaxError as exc:
            if not untagged:
                unread[fence.opener] = str(exc)
            continue
        found.update(body[number - 1] for number in numbers)
    return found, unread


def first_word_file_command_lines(fence: Fence, lines: list[str]) -> set[int]:
    """The indices of the body lines of a FIRST_WORD_FENCES fence whose first word names one of SHELL_FILE_COMMANDS,
    each line as CommonMark gives it to the fence's content. A session runs the lines session_commands reads, each
    without its prompt; any other fence runs every line, after its blanks and any `$ ` or `> ` prompt, since the other
    labels name a script. A continuation line's first word is read as a command name though it may be an argument of
    the command it continues, as `  head` after `$ git log --author \\`. A first word is matched in any case in an
    ANY_CASE_FENCES fence, and as written otherwise."""
    any_case = fence.language.casefold() in ANY_CASE_FENCES
    return {
        index
        for index, text in first_word_commands(fence, lines)
        if first_word_runs_file_command(text, any_case=any_case)
    }


def first_word_commands(fence: Fence, lines: list[str]) -> list[tuple[int, str]]:
    """The command lines of a fence read by its first word, by index, each as CommonMark gives it to the fence's
    content without its prompt: a session's, as session_commands reads them, and otherwise every body line, after its
    blanks and any `$ ` or `> ` prompt."""
    commands = session_commands(fence, lines)
    if commands is None:
        commands = [(index, PROMPT.sub("", fence.content(lines[index]), count=1)) for index in fence.body()]
    return commands


def first_word_runs_file_command(command: str, *, any_case: bool) -> bool:
    """Whether a command of a FIRST_WORD_FENCES fence, without its prompt, starts with one of SHELL_FILE_COMMANDS
    after its blanks, by its file name without `.exe`: in any case when `any_case` is set, as PowerShell and cmd find
    a program, and as written otherwise, as Bash and fish match a command."""
    words = command.split()
    if not words:
        return False
    name = program_name(PureWindowsPath(words[0]).name)
    return (name.casefold() if any_case else name) in SHELL_FILE_COMMANDS


def open_here_document(text: str) -> bool:
    """Whether a here-document in a Bash text runs to the text's end, with no delimiter line closing it."""
    return any(
        not document.closed
        for command in bash_syntax.every_command(text)
        for document in bash_syntax.here_documents(command)
    )


def file_command_lines(text: str) -> set[int]:
    """The line each command Bash runs for a text that runs one of SHELL_FILE_COMMANDS starts on, counted from 1. Raises
    BashSyntaxError where the grammar refuses the text, or the script eval, a shell's -c, or a trap's action runs in
    it."""
    return {
        line for line, words in bash_programs(text) if command_wrappers.bash_program(words[0]) in SHELL_FILE_COMMANDS
    }


def bash_programs(text: str) -> list[tuple[int, list[str]]]:
    """Each program a Bash text runs, as the one Bash grammar reads it, with the line its command starts on, counted
    from 1, and its words from the program's name, each part only running settles as git_subcommands' UNREAD_PART:
    every simple command's, wherever Bash starts one, past its assignments, and the program each wrapper of
    command_wrappers' catalogue runs, and each the script eval, a shell's -c, or a trap's action runs, on the line of
    the command that runs it. Raises BashSyntaxError where the grammar refuses the text, or such a script: an eval
    script or trap action the grammar refuses, which Bash refuses only when it runs it, is refused as the script's own
    text would be."""
    return [found for command in bash_syntax.simple_commands(text) for found in simple_command_programs(command)]


def simple_command_programs(command: Simple) -> list[tuple[int, list[str]]]:
    """Each program a simple command runs, as command_programs reads it from the command's words, each part only
    running settles as git_subcommands' UNREAD_PART."""
    return command_programs(command, bash_syntax.command_words(command, git_subcommands.UNREAD_PART))


def command_programs(command: Simple, words: list[str]) -> list[tuple[int, list[str]]]:
    """Each program a simple command runs, given its words from its program's name, as `bash_programs` reads it, and
    each the action a trap sets runs, read as eval's script is, a part only running settles as one word; a wrapper
    whose program no literal names, and a -c script that is UNREAD_PART alone, run none this can see."""
    found: list[tuple[int, list[str]]] = []
    while words:
        found.append((command.line, words))
        head, rest = words[0], words[1:]
        name = command_wrappers.bash_program(head)
        if head == "eval":
            return found + [(command.line, inner) for _, inner in bash_programs(" ".join(rest))]
        if head == "trap":
            action = command_wrappers.trap_action(rest)
            return found + ([] if action is None else [(command.line, inner) for _, inner in bash_programs(action)])
        if name in command_wrappers.SCRIPT_SHELLS:
            index = command_wrappers.shell_script_index(rest)
            if index is None or rest[index] == git_subcommands.UNREAD_PART:
                return found
            return found + [(command.line, inner) for _, inner in bash_programs(rest[index])]
        words = command_wrappers.unwrapped([name, *rest])
        if words and words[0].startswith(command_wrappers.UNREAD_PROGRAM):
            break
    return found


def delegation_lines(
    path: Path, lines: list[str], languages: list[str | None], start: int
) -> tuple[list[str], list[int]]:
    """Delegations whose reply is never bounded, and subagent prompts that a script writes at runtime, with the line
    number of each runtime prompt carried beside the printed lines, never read back from them: a path holding a space
    would move every later field."""
    output: list[str] = []
    prompts: list[int] = []
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
        prompts.append(index + 1)
    return output, prompts


def skill_folder(path: Path) -> Path | None:
    """The skill folder a scanned file belongs to: the nearest folder above it that holds SKILL.md, named as the
    file is, so a relative path stays relative."""
    for folder in path.parents:
        folder = folder if folder.name else opened_path(folder)
        if main_files(folder):
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
        samples = listed_entries(samples_folder, "*.txt") if is_directory(samples_folder) else []
        if not samples:
            output.append(f"RUNTIME_PROMPT_UNSAMPLED {posix(path)} {line} no sample under {posix(samples_folder)}")
        elif samples_folder not in measured:
            measured.add(samples_folder)
            for sample in samples:
                output.extend(measure_sample(root, sample))
    return output


def measure_sample(root: Path, sample: Path) -> list[str]:
    """A rendered prompt's estimate, the source files it has the subagent read, and whether it bounds the reply."""
    text = required_text(sample, outside_bytes)
    output = [f"RUNTIME_PROMPT_SAMPLE {posix(sample)} {estimate_tokens(text, 'doc')}"]
    named = {match.group(1).replace("\\", "/").rstrip(".,;:)") for match in SAMPLE_SOURCE_PATH.finditer(text)}
    for relative in sorted(named):
        target = root / relative
        if target.suffix.casefold() not in HELPER_SUFFIXES and is_outside_file(target):
            tokens = estimate_tokens(required_text(target, outside_bytes), "doc")
            output.append(f"RUNTIME_PROMPT_READ {posix(sample)} {relative} {tokens}")
    output.append(f"REPLY_BOUNDED {posix(sample)} {'yes' if REPLY_BOUND.search(text) else 'no'}")
    return output + sample_shape_cues(sample, text_lines(text))


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
    # each exit whose code is a name no constant reading reaches, by its line and the name as written
    unread: list[tuple[int, str]] = field(default_factory=list)


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


def no_modules(_dotted: str) -> exit_constants.ModuleConstants | None:
    """A module locator that finds none, so a constant only an import names is unread."""
    return None


# What binds a name in the scope that holds it by a name field: a definition, an except clause, or a match capture.
NAMED_BINDINGS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.ExceptHandler, ast.MatchAs, ast.MatchStar)


def local_names(function: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> set[str]:
    """The names a function or lambda binds in its own scope: its parameters and what its own body stores, outside the
    functions, lambdas, and classes it defines, but the names it declares global or nonlocal."""
    arguments = function.args
    names = {
        each.arg
        for each in [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs, arguments.vararg, arguments.kwarg]
        if each is not None
    }
    declared: set[str] = set()
    pending: list[ast.AST] = [*ast.iter_child_nodes(function)]
    while pending:
        node = pending.pop()
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store | ast.Del):
            names.add(node.id)
        elif isinstance(node, ast.Global | ast.Nonlocal):
            declared.update(node.names)
        elif isinstance(node, NAMED_BINDINGS) and node.name:
            names.add(node.name)
        if not isinstance(node, exit_constants.SCOPE_NODES):
            pending.extend(ast.iter_child_nodes(node))
    return names - declared


def _runs_around(node: ast.AST, holder: ast.AST) -> bool:
    """Whether a node of a definition runs in the scope around it, outside its type parameters' scope: in a decorator,
    or in a parameter's default."""
    if not isinstance(holder, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return False
    around: list[ast.expr | None] = [*holder.decorator_list]
    if not isinstance(holder, ast.ClassDef):
        around += [*holder.args.defaults, *holder.args.kw_defaults]
    return any(inner is node for item in around if item is not None for inner in ast.walk(item))


class ExitReading:
    """The codes one script's exit expressions give: each literal, and each name a constant binds, read by
    ejryan89-skill-core's exit_constants through the module's imports and the modules `locate` finds; each name no
    such reading reaches is recorded as unread, by its line and the name as written."""

    def __init__(self, tree: ast.Module, locate: exit_constants.Locate) -> None:
        self.aliases = import_aliases(tree)
        self.constants = exit_constants.read(tree, self.aliases)
        self.locate = locate
        self.parents = {id(child): node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        self.locals_of: dict[int, set[str]] = {}
        self.unread: dict[tuple[int, str], None] = {}

    def is_local(self, node: ast.Name) -> bool:
        """Whether a function or lambda that holds the name binds it itself, or a function, class, or type statement
        that holds it binds it as a type parameter, outside the decorators and the defaults, which run around it."""
        holder = self.parents.get(id(node))
        while holder is not None:
            if node.id in type_parameter_names(holder) and not _runs_around(node, holder):
                return True
            if isinstance(holder, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
                if id(holder) not in self.locals_of:
                    self.locals_of[id(holder)] = local_names(holder)
                if node.id in self.locals_of[id(holder)]:
                    return True
            holder = self.parents.get(id(holder))
        return False

    def literal(self, node: ast.expr) -> list[int]:
        """The codes an exit expression gives: both arms of a conditional, an int literal, or a constant's values."""
        if isinstance(node, ast.IfExp):
            return self.literal(node.body) + self.literal(node.orelse)
        if isinstance(node, ast.Constant):
            return [node.value] if type(node.value) is int else []
        root = node
        while isinstance(root, ast.Attribute):
            root = root.value
        # Only a dotted name names a constant, and one that starts at a name a function binds is that function's.
        if not isinstance(root, ast.Name) or self.is_local(root):
            return []
        values = exit_constants.resolve(node, self.constants, self.locate)
        if values is None:
            self.unread[(node.lineno, ast.unparse(node))] = None
            return []
        return sorted(values)


def exit_codes(tree: ast.Module, locate: exit_constants.Locate = no_modules) -> tuple[set[int], list[tuple[int, str]]]:
    """The literal codes a script exits with, and each exit whose code is a name it cannot read, by its line and the
    name as written: what a function annotated to return an int returns, and what an exit call or SystemExit takes,
    read by ejryan89-skill-core's exit_calls as validation's exit tables read it: no code, or a SystemExit raised as
    the class, exits 0, a message exits 1, a call Python refuses for its arguments, such as `sys.exit(code=4)`, exits
    1, and otherwise a number or a name a constant binds, read by ejryan89-skill-core's exit_constants as validation's
    exit tables read it: an int literal bound at the module's top or in a module-level block, annotated or not, or one
    an import names in a module `locate` finds. A name no such reading reaches is unread, never a code the script does
    not exit with; a name a function binds itself, a local or a parameter, is not read. A call is an exit call by the
    dotted name the module's imports resolve it to, so `from sys import exit as leave` is one and a method such as
    `smtp.quit()` or `parser.exit()` is not."""
    reading = ExitReading(tree, locate)
    aliases = reading.aliases
    codes: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and isinstance(node.returns, ast.Name) and node.returns.id == "int":
            for inner in ast.walk(node):
                if isinstance(inner, ast.Return) and inner.value is not None:
                    codes.update(reading.literal(inner.value))
        elif isinstance(node, ast.Raise) and node.exc is not None and not isinstance(node.exc, ast.Call):
            raised = qualified_name(node.exc, aliases)
            codes.update([exit_calls.NO_CODE] if raised == exit_calls.SYSTEM_EXIT else [])
        elif isinstance(node, ast.Call) and (function := qualified_name(node.func, aliases)) in exit_calls.EXIT_CALLS:
            fixed = exit_calls.call_code(node, function)
            if fixed is not None:
                codes.add(fixed)
            elif (argument := exit_calls.exit_argument(node, function)) is not None:
                codes.update(reading.literal(argument))
    return codes, list(reading.unread)


def python_facts(text: str, locate: exit_constants.Locate = no_modules) -> ScriptFacts:
    """What a Python script prints and exits with, following an exit code an import names into each module `locate`
    finds."""
    tree = python_reader.parse(text)
    facts = ScriptFacts()
    docstrings = docstring_ids(tree)
    parents = {id(child): node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            string_facts(facts, node, node.value, parents)
        elif isinstance(node, ast.Constant) and isinstance(node.value, bytes):
            string_facts(facts, node, node.value.decode("latin-1"), parents)
    codes, facts.unread = exit_codes(tree, locate)
    facts.codes |= codes
    names = {
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import | ast.ImportFrom) for alias in node.names
    }
    if "run_main" in names:
        facts.words.add("FAILED")
        facts.codes |= RUN_MAIN_CODES
    if "argparse" in names:
        facts.codes.add(USAGE_CODE)
    return facts


def shell_facts(text: str, suffix: str) -> ScriptFacts:
    """What a shell script prints and exits with: a Bash script's as `bash_facts` reads it, and a PowerShell script's
    from each of its lines outside comment lines, which its tokenizer ends at CR, LF, and CRLF, as `text_lines` splits
    them."""
    if suffix.casefold() != ".ps1":
        return bash_facts(text)
    facts = ScriptFacts()
    for number, line in enumerate(text_lines(text), start=1):
        if line.lstrip().startswith("#"):
            continue
        facts.words.update(ANY_STATUS_WORD.findall(line))
        for match in SHELL_PRINT.finditer(line):
            facts.lines.setdefault(match.group(1), number)
        # A code past the digit limit, or in digits other than ASCII, is no code a shell exits with.
        codes = (whole_number.parse(code) for code in SHELL_EXIT.findall(line))
        facts.codes.update(code for code in codes if code is not None)
    return facts


def bash_facts(text: str) -> ScriptFacts:
    """What a Bash script prints and exits with, read through the one Bash grammar, so a comment holds nothing and a
    line ends at LF alone: the status words of every word each command expands, a function's name, and each
    here-document's body; the status word each echo or printf starts its first word after its options with, on the
    line its command starts on; and the code each exit command names, each wherever a command runs, in a substitution,
    past a wrapper, and in the script eval, a shell's -c, or a trap's action runs. Raises BashSyntaxError where the
    grammar refuses the script."""
    facts = ScriptFacts()
    for command in bash_syntax.every_command(text):
        if isinstance(command, bash_syntax.FunctionDefinition):
            facts.words.update(ANY_STATUS_WORD.findall(command.name or ""))
            continue
        for word in bash_syntax.expanded_words(command):
            facts.words.update(ANY_STATUS_WORD.findall(bash_syntax.word_text(word)))
        for document in bash_syntax.here_documents(command):
            facts.words.update(ANY_STATUS_WORD.findall(document.body))
    for line, words in bash_programs(text):
        if command_wrappers.bash_program(words[0]) in BASH_PRINTS:
            printed = [PRINTED_STATUS.match(word) for word in dropwhile(PRINT_OPTION.fullmatch, words[1:])][:1]
            if printed and printed[0]:
                facts.lines.setdefault(printed[0].group(1), line)
        # A code past the digit limit, or in digits other than ASCII, is no code a shell exits with.
        elif words[0] == "exit" and len(words) > 1 and (code := whole_number.parse(words[1])) is not None:
            facts.codes.add(code)
    return facts


def script_facts(path: Path, text: str | None = None, locate: exit_constants.Locate = no_modules) -> ScriptFacts:
    """What a script prints and exits with, from its text, which the caller read or `require_text` reads, a Python
    script's exit codes followed into each module `locate` finds. One that is not UTF-8 text, not valid Python, or Bash
    the grammar refuses, fails: what it prints cannot be measured, and reading it as printing nothing would compare the
    Markdown with less than the skill runs."""
    if text is None:
        text = require_text(path)
    if path.suffix.casefold() != ".py":
        try:
            return shell_facts(text, path.suffix)
        except BashSyntaxError as exc:
            raise InputError(f"cannot read {posix(path)}: not Bash the grammar reads ({exc})") from exc
    try:
        return python_facts(text, locate)
    except python_reader.CONTENT_ERRORS as exc:
        raise InputError(f"cannot read {posix(path)}: not valid Python") from exc


def python_constants(texts: dict[Path, str]) -> dict[Path, exit_constants.ModuleConstants]:
    """The constants of each Python script of `texts` that parses, by the path the runtime opens it at; one that does
    not parse is left to script_facts to fail on."""
    found: dict[Path, exit_constants.ModuleConstants] = {}
    for path, text in texts.items():
        if path.suffix.casefold() != ".py":
            continue
        try:
            tree = python_reader.parse(text)
        except python_reader.CONTENT_ERRORS:
            continue
        found[opened_path(path)] = exit_constants.read(tree, import_aliases(tree))
    return found


def module_locator(
    path: Path, folders: list[Path], modules: dict[Path, exit_constants.ModuleConstants]
) -> exit_constants.Locate:
    """Find a module a script at `path` imports, by its dotted name, as Python finds it from the script's own folder and
    then from `folders`, ejryan89-skill-core's scripts, which a skill's script puts on its path: among the scripts the
    audit read alone, so a module outside them is not found and a constant it names is unread. The scan reads a
    folder's own files and none of its subfolders', so a package's `__init__.py` and its modules are among them only
    when a command runs one or sits beside one it runs; otherwise a constant a package holds is unread."""

    def locate(dotted: str) -> exit_constants.ModuleConstants | None:
        relative = Path(*dotted.split("."))
        for folder in (path.parent, *folders):
            for candidate in (folder / relative.with_suffix(".py"), folder / relative / "__init__.py"):
                found = modules.get(opened_path(candidate))
                if found is not None:
                    return found
        return None

    return locate


def repository_root(folder: Path) -> Path | None:
    """The nearest folder above a skill that holds a .git entry, where its commands run tools from, or None; a .git
    that is a link to nothing is none, since git reads nothing through it, and a .git entry it cannot look at raises
    the stat's OSError rather than passing to the folder above."""
    return next((parent for parent in opened_path(folder).parents if is_there(parent / ".git")), None)


def is_script_name(path: Path) -> bool:
    """Whether a listed name is a script whose output the audit compares: not a regression suite, no skill's output."""
    return path.suffix.casefold() in STATUS_SCRIPT_SUFFIXES and not is_suite_name(path.name)


def skill_scripts(skill: Path, folder: Path) -> list[Path]:
    """The scripts of `folder`, a folder of the skill folder `skill` as it names it, resolved, through a reader bound to
    the skill folder: a folder, or a script, that is a link leading out of it is passed over, as the walk passes one."""
    reader = TreeReader(skill)
    if leads_out(reader, folder) or not reader.folder(folder):
        return []
    return [
        opened_path(path) for path in reader.listing(folder) if is_script_name(path) and is_listed_file(reader, path)
    ]


def outside_scripts(folder: Path) -> list[Path]:
    """`skill_scripts` of a folder outside the examined skill, which the status lines read by design, as
    TREE_READ_ALLOWED states, following its links."""
    if not is_directory(folder):
        return []
    return [opened_path(path) for path in listed(folder) if is_script_name(path) and is_outside_file(path)]


def passed_over(skill: Path, script: Path) -> bool:
    """Whether a script a command names lies in the skill folder by its text, as named, which the folder's reader does
    not touch, as when a link on its way leads out of the folder: such a script is passed over unread, as the walk
    passes one, never read where the link leads. The folder is judged as the audit names it and as it opens."""
    named = lexical_absolute(script)
    for folder in dict.fromkeys((skill, opened_path(skill))):
        reader = TreeReader(folder)
        if within(named, reader.tree):
            return reader.refusal(script) is not None
    return False


def in_skill(skill: Path, path: Path) -> Path | None:
    """`path`, as the runtime opens it (`opened_path`), named under the skill folder `skill` as the audit names it,
    when it lies in the folder, or None outside it."""
    root = opened_path(skill)
    return skill.joinpath(*path.parts[len(root.parts) :]) if within(path, root) else None


def scripts_beside(skill: Path, folder: Path) -> list[Path]:
    """The scripts of `folder`, resolved: those of a folder of the skill through its reader (`skill_scripts`), and
    those of one outside it, beside a script a command runs from the repository root or another skill, or
    ejryan89-skill-core's, as the outside read it is by design (`outside_scripts`)."""
    inside = in_skill(skill, opened_path(folder))
    return skill_scripts(skill, inside) if inside is not None else outside_scripts(folder)


def named_bytes(skill: Path, path: Path) -> bytes:
    """The bytes of a file a command of the skill folder `skill` runs, at `path` as the runtime opens it
    (`opened_path`): through a reader bound to the skill folder when it lies there, and as the outside read it is by
    design otherwise, a script a command runs from the repository root or another skill, or ejryan89-skill-core's."""
    inside = in_skill(skill, path)
    return outside_bytes(path) if inside is None else TreeReader(skill).contents(inside)


def script_text(skill: Path, path: Path) -> str:
    """The text of a script the status lines or the runtime needs read, at `path` as the runtime opens it
    (`named_bytes`)."""
    return required_text(path, lambda named: named_bytes(skill, named))


def script_is_present(script: Path) -> bool:
    """Whether a script a command names is there, a name the platform answers EINVAL for being one no file holds."""
    try:
        return is_present(script)
    except NotAFile:
        raise
    except OSError as exc:
        if exc.errno == errno.EINVAL:
            return False
        raise


def command_scripts(
    folder: Path, root: Path | None, markdown: list[tuple[Path, list[str]]]
) -> tuple[list[Path], list[str]]:
    """The scripts the Markdown's commands run, through the skill-directory variable or from the repository root, as a
    repository skill runs a tool: those that exist, and a SCRIPT_MISSING line for each command naming one that does not.
    A script named in the skill folder that a link leads out of is passed over (`passed_over`). A directory in a
    script's place raises file_presence's NotAFile, and a stat that fails raises its OSError; a name the platform cannot
    hold, such as one with a `?` on Windows, names no file, so it is missing."""
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
                if passed_over(folder, script):
                    continue
                if script_is_present(script):
                    found.append(opened_path(script))
                # A bare name from the repository root, such as a value a span gives, names no tool's path.
                elif text.startswith(SKILL_DIR_PREFIX) or "/" in text:
                    missing.append(f"SCRIPT_MISSING {posix(path)} {line} {text}")
    return list(dict.fromkeys(found)), list(dict.fromkeys(missing))


# The containment tests that are not decisions, each with why it does not call path_containment.
CONTAINMENT_ALLOWED = {
    "shown_path": "display only: a path outside the skill folder is shown resolved rather than refused",
}


def shown_path(folder: Path, path: Path) -> str:
    """A script's path under the skill folder as it was given, or its resolved path outside it."""
    try:
        return posix(folder / path.relative_to(opened_path(folder)))
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
        for span in code_spans(line):
            word = SPAN_STATUS.match(span.text.strip())
            if (
                word
                and len(word.group(1)) >= STATUS_WORD_LENGTH
                and not EXAMPLE_LEAD.search(line[: span.start])
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


def stated_codes(path: Path, lines: list[str], codes: set[int], unread: bool = False) -> list[str]:
    """An EXIT_MISMATCH line for each exit code a prose line states that no script exits with, or, when some exit's
    code is a name the scripts' reading could not follow, an EXIT_UNREAD line, since the code may be that one."""
    languages, _ = parse_fences(lines)
    return [
        f"EXIT_UNREAD {posix(path)} {index + 1} {code}" if unread else f"EXIT_MISMATCH {posix(path)} {index + 1} {code}"
        for index in range(body_start(lines), len(lines))
        if languages[index] is None
        for match in EXIT_STATED.finditer(lines[index])
        if (code := stated_code(match.group(1) or match.group(2))) not in codes
    ]


def status_lines(folder: Path) -> list[str]:
    """Compare the status lines and exit codes a skill's Markdown names with what its scripts print and exit with.

    The scripts read are the skill's own, every script its commands run and the scripts beside it, and
    ejryan89-skill-core's. UNPRINTED is a status word a code span names on a prose line about output that none of them
    prints; EXIT_MISMATCH an exit code the prose states that none of them uses, and EXIT_UNREAD one that none of the
    exits they read gives when some exit's constant is unread, so the code may be that one; UNEXPLAINED a line that a
    script the skill runs, its own or a repository tool, starts with a status word the Markdown never names;
    SCRIPT_MISSING a command naming a script that does not exist. A skill with no script of its own that runs none has
    nothing else to compare.
    """
    markdown = skill_markdown(folder)
    root = repository_root(folder)
    bodies = [lines[body_start(lines) :] for _, lines in markdown]
    commands, missing = command_scripts(folder, root, markdown)
    own = skill_scripts(folder, folder / "scripts")
    if not own and not commands:
        return missing
    cores = [
        folder.parent / "ejryan89-skill-core" / "scripts",
        *([root / "skills" / "ejryan89-skill-core" / "scripts"] if root else []),
    ]
    reading = [*own, *commands, *(path for command in commands for path in scripts_beside(folder, command.parent))]
    reading += [path for core in cores for path in scripts_beside(folder, core)]
    skills_roots = {opened_path(folder).parent, *((root / "skills", root / ".claude" / "skills") if root else ())}
    runs = [
        path
        for path in commands
        if within(path, opened_path(folder), strict=True)
        or not any(within(path, skills, strict=True) for skills in skills_roots)
    ]
    facts = ScriptFacts()
    first_lines: dict[str, tuple[Path, int]] = {}
    texts = {path: script_text(folder, path) for path in dict.fromkeys(reading)}
    modules = python_constants(texts)
    for path, text in texts.items():
        found = script_facts(path, text, module_locator(path, cores, modules))
        facts.words |= found.words
        facts.prefixes |= found.prefixes
        facts.codes |= found.codes
        facts.unread += found.unread
        if path in runs:
            for word, number in found.lines.items():
                first_lines.setdefault(word, (path, number))
    output = [
        line for path, lines in markdown for line in stated_lines(folder, path, lines, facts.words, facts.prefixes)
    ]
    output += [line for path, lines in markdown for line in stated_codes(path, lines, facts.codes, bool(facts.unread))]
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
    """A skill folder's SKILL.md and every doc file inventory lists, each with its lines, read through a reader bound
    to the folder; one that is not UTF-8 text fails, as the module docstring says tools and scan fail on one."""
    reader = TreeReader(folder)
    return [(path, text_lines(require_text(path, reader))) for path in scanned_files(folder)]


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
    return {f"{prefix}{path.stem}": path for path in listed_entries(root, "*.md")} if is_directory(root) else {}


def line_agents(line: str, defined: set[str]) -> list[str]:
    """The subagent types a prose line names: a type a delegation line names by TYPED_AGENT, and any code span that is
    the name of a defined agent."""
    found = [name for match in TYPED_AGENT.finditer(line) for name in match.groups() if name] if delegates(line) else []
    return found + [span.text.strip() for span in code_spans(line) if span.text.strip() in defined]


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
    """One command handler of a hooks file, read as agent_hooks reads a definition's, and a command the plugin build
    wrote to start Python in every shell as the Python it starts."""
    hook = Hook(event, group)
    command = handler.get("command")
    if isinstance(command, str) and skill_menu.plugin_hook_arguments(command) is not None:
        hook.program = skill_menu.PLUGIN_PYTHON
    elif isinstance(command, str) and command.split():
        hook.program = unquoted(command.split()[0])
    hook.timeout = str(handler["timeout"]) if "timeout" in handler else hook.timeout
    hook.on_failure = str(handler["onFailure"]) if "onFailure" in handler else hook.on_failure
    return hook


def hooks_file_hooks(path: Path, document: dict[str, object]) -> list[tuple[Hook, str]]:
    """Each command hook a plugin's hooks document holds, with its matcher, as Claude Code reads the file: `hooks` an
    object of events, each a list of matcher groups, each an object whose optional matcher is text and whose `hooks`
    is a list of handler objects, each naming its type as text and, for a command, its command as text. A member of
    any other kind raises InputError naming it, since the hooks Claude Code would run cannot be known without it."""

    def refused(member: str) -> InputError:
        return InputError(f"{posix(path)} is not a hooks file Claude Code reads: {member}")

    events = document.get("hooks")
    if events is None:
        return []
    if not isinstance(events, dict):
        raise refused("'hooks' must be an object mapping event names to matcher groups")
    found: list[tuple[Hook, str]] = []
    group = 0
    for event, groups in events.items():
        if not isinstance(groups, list):
            raise refused(f"hook event '{event}' must be a list of matcher groups")
        for entry in groups:
            group += 1
            if not isinstance(entry, dict):
                raise refused(f"each matcher group of hook event '{event}' must be an object")
            matcher, handlers = entry.get("matcher"), entry.get("hooks")
            if matcher is not None and not isinstance(matcher, str):
                raise refused(f"a matcher of hook event '{event}' must be text")
            if not isinstance(handlers, list):
                raise refused(f"each matcher group of hook event '{event}' needs a 'hooks' list")
            for handler in handlers:
                kind = handler.get("type") if isinstance(handler, dict) else None
                if not isinstance(handler, dict) or not isinstance(kind, str):
                    raise refused(f"each handler of hook event '{event}' must be an object naming its type as text")
                if kind != "command":
                    continue
                if not isinstance(handler.get("command"), str):
                    raise refused(f"a command hook of hook event '{event}' needs its command as text")
                found.append((json_hook(event, group, handler), matcher or "*"))
    return found


def plugin_hooks(folder: Path) -> list[tuple[Hook, str]]:
    """Each command hook the hooks file of the skill's plugin holds, with its matcher; none outside a plugin. A hooks
    file that cannot be read, is not JSON, is not a JSON object, or holds a member of another kind than Claude Code
    reads raises InputError."""
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
        document = json_reader.loads(required_text(path, outside_bytes))
    # ValueError covers a decode error, UnicodeError, an over-long integer, and nesting json_reader refuses
    except ValueError as exc:
        raise InputError(f"{posix(path)} is not JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise InputError(f"{posix(path)} is not a JSON object")
    return hooks_file_hooks(path, document)


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
        definition = read_agent(path) if name not in guarded and is_outside_file(path) else None
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
    # ValueError covers a decode error, UnicodeError, an over-long integer, and nesting json_reader refuses
    except ValueError:
        return []
    found: list[tuple[str, Path, int, str]] = []
    # Where each key and value pair is first written, and where each line starts, counted at LF alone, read once.
    places: dict[tuple[str, str], int] = {}
    for pair in JSON_STRING_PAIR.finditer(text):
        places.setdefault((pair.group(1), pair.group(2)), pair.start())
    starts = line_starts(text, LINE_FEED)
    stack: list[object] = [data]
    while stack:
        node = stack.pop()
        items = node.items() if isinstance(node, dict) else enumerate(node) if isinstance(node, list) else []
        for key, value in items:
            kind = setting_kind(key, value) if isinstance(key, str) else None
            if kind is not None and isinstance(key, str) and isinstance(value, str):
                where = places.get((key, value))
                found.append((kind, path, line_index(starts, where) + 1 if where is not None else 1, value))
            elif isinstance(value, dict | list):
                stack.append(value)
    return sorted(found, key=lambda item: item[2])


def python_settings(path: Path, text: str) -> list[tuple[str, Path, int, str]]:
    """Each model or effort a script sets as a literal: a dict entry or a keyword argument named for one."""
    try:
        tree = python_reader.parse(text)
    except python_reader.CONTENT_ERRORS:
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
        folder.parent / name
        for name in dict.fromkeys(found)
        if name != folder.name and is_directory(folder.parent / name, other_is_absent=True)
    ]


def data_settings(folders: list[Path]) -> list[tuple[str, Path, int, str]]:
    """The models and efforts the skill's and its dependencies' files set for a subagent: a reference's frontmatter, as
    a reviewer profile's, a JSON data file, or a script's literal. A doc file that is not UTF-8 text fails, as the
    skill loads it; a helper or data file that is not sets nothing, since it is measured rather than loaded."""
    found: list[tuple[str, Path, int, str]] = []
    for folder in folders:
        reader = TreeReader(folder)
        for path in folder_files(folder):
            relative = path.relative_to(folder)
            kind = category(relative, folder)
            if kind == "main" or is_suite_name(path.name):
                continue
            text = require_text(path, reader) if kind == "doc" else measured(reader, path, kind)[1]
            if text is None:
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
    lines = text_lines(text)
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
            pinned += markdown_settings(definition.path, required_text(definition.path, outside_bytes))
    pinned += data_settings([folder, *dependency_folders(folder, markdown)])
    output += [
        f"PINNED_SUBAGENT {posix(path)} {line} {kind} {value}"
        for kind, path, line, value in dict.fromkeys(pinned)
        if value.strip().casefold() != "inherit"
    ]
    return output


# --- runtime support ------------------------------------------------------------------------------


def powershell_script_commands(script: str) -> list[list[str]]:
    """The words of each external command one PowerShell script runs, as ejryan89-skill-core's powershell_commands
    reads them with pwsh's parser, which repository validation's reader runs too. A script pwsh cannot read fails."""
    try:
        return powershell_commands.read([script])[0]
    except powershell_commands.PowerShellUnread as exc:
        raise InputError(f"cannot read a PowerShell script's commands: {exc}") from exc


def script_git_subcommands(script: Path, text: str | None = None) -> set[str]:
    """The subcommand of each git command a Bash or PowerShell script runs, by ejryan89-skill-core's git_subcommands,
    the reader repository validation shares, or of each git call a Python script makes, by the shared derivation; any
    other script runs none this reads. A Python script that does not parse fails, as script_facts fails it, and so does
    a Bash script the Bash grammar refuses, the script its eval, a shell's -c, or a trap's action runs included, a
    PowerShell script whose Bash shell runs such a script, and a PowerShell script, or a script a Bash script starts
    PowerShell on, that pwsh cannot read; each failure names the script. `text` is the script's, which the caller
    read or `require_text` reads."""
    if text is None:
        text = require_text(script)
    suffix = script.suffix.casefold()
    if suffix in (".sh", ".bash"):
        try:
            return git_subcommands.bash_git_subcommands(text, powershell_script_commands)
        except BashSyntaxError as exc:
            raise InputError(f"cannot read {posix(script)}: not Bash the grammar reads ({exc})") from exc
    if suffix == ".ps1":
        commands = powershell_script_commands(text)
        try:
            return git_subcommands.powershell_git_subcommands(commands, powershell_script_commands)
        except BashSyntaxError as exc:
            raise InputError(
                f"cannot read {posix(script)}: a Bash script it runs is not Bash the grammar reads ({exc})"
            ) from exc
    if suffix != ".py":
        return set()
    try:
        return runtime_support.python_git_subcommands(text)
    except python_reader.CONTENT_ERRORS as exc:
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
    for script in skill_scripts(folder, folder / "scripts"):
        for subcommand in sorted(script_git_subcommands(script, script_text(folder, script))):
            needs = runtime_support.frontmatter_needs([], False, git_subcommands=frozenset({subcommand}))
            evidence += [(need, f"git {subcommand} {posix(script.relative_to(opened_path(folder)))}") for need in needs]
    return list(dict.fromkeys(evidence))


def own_needs(folder: Path) -> set[str]:
    """The runtime capabilities a skill's own frontmatter, metadata, and scripts show it needs, as needs_evidence reads
    them for the skill audited."""
    path = folder / "SKILL.md"
    try:
        # The entry the folder gives for SKILL.md, read under the name it is listed by.
        path = next(iter(main_files(folder)), path)
    except OSError as exc:
        raise InputError(f"cannot read {posix(path)}: {exc}") from exc
    # Read through the folder's reader, a path it refuses or a file it does not read named as required_text names one.
    text = required_text(path, TreeReader(folder).contents)
    try:
        found = frontmatter.split(text_lines(text))
        if found is None:
            raise frontmatter.FrontmatterError("no frontmatter")
        document = frontmatter.Frontmatter(found[0])
        granted = parse_allowed_tools(document.value("allowed-tools")) or []
        listed = listing(document)
    except frontmatter.FrontmatterError as exc:
        raise InputError(f"cannot read {posix(path)}: {exc}") from exc
    metadata = read_metadata(folder.parent.parent, folder.name)
    if metadata is None and unreadable_metadata(folder.parent.parent, folder.name):
        raise InputError(
            f"cannot read {posix(metadata_path(folder.parent.parent, folder.name))}: not metadata it reads"
        )
    tools = names(metadata, "tools") + names(metadata, "optional_tools")
    return {need for need, _ in needs_evidence(folder, granted, "INVOCATION model" not in listed, tools)}


def skill_folders(skills: Path) -> list[Path]:
    """The folders a skills folder holds, the skill audited and its siblings, which it may invoke: a place outside the
    examined folder, listed by design, as TREE_READ_ALLOWED states; each folder's own files are read through a reader
    bound to it."""
    return [child for child in sorted(skills.iterdir()) if is_directory(child, other_is_absent=True)]


def invoked_evidence(folder: Path) -> list[tuple[str, str]]:
    """Each runtime capability a skill the audited one invokes carries over to it, with that skill, by
    ejryan89-skill-core's runtime_support derivation, which repository validation shares."""
    skills = folder.parent
    # Each sibling by the entry its folder gives for SKILL.md, as the runtime that opens it finds it, read under the
    # name it is listed by, through a reader bound to the sibling's folder.
    main = {child.name: entry for child in skill_folders(skills) for entry in main_files(child)}

    def invokes(name: str) -> set[str]:
        if name not in main:
            return set()
        text = require_text(main[name], TreeReader(skills / name))
        return runtime_support.invoked_skills(text, set(main))

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
        unreadable = root is not None and unreadable_metadata(root, folder.name)
        return ["RUNTIME_UNREADABLE its metadata cannot be read"] if unreadable else []
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


def user_home() -> Path:
    """The user's home folder, or InputError naming its variable where it is not an absolute path, so no skill is
    looked for under the working directory."""
    try:
        return absolute_home()
    except RelativeEnvironmentPath as error:
        raise InputError(f"{error}; pass --home") from None


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    find = commands.add_parser("locate", help="find a skill's main file")
    find.add_argument("name", nargs="?", default="", help="[<plugin>:]<skill>, then optionally a plugin folder")
    find.add_argument("--repo", type=Path, help="project root; default: the Git root of the current directory")
    find.add_argument("--home", type=Path, help="the home folder to search; default: the user's own")
    commands.add_parser("inventory", help="size every file of a skill").add_argument("directory", type=Path)
    commands.add_parser("tools", help="compare referenced tools with allowed-tools").add_argument("file", type=Path)
    commands.add_parser("scan", help="list cost cues").add_argument("paths", type=Path, nargs="+")
    commands.add_parser("provenance", help="name the guidance a check derives from").add_argument("checks", nargs="+")
    args = parser.parse_args(arguments)
    try:
        if args.command == "locate":
            repo = given_path(args.repo) if args.repo is not None else git_toplevel(Path.cwd())
            # The argument as the user gave it: a name, then a plugin folder after the first space.
            name, _, folder = args.name.strip().partition(" ")
            home = given_path(args.home) if args.home is not None else user_home()
            output = locate(name, home, repo, given_path(Path(folder.strip())) if folder.strip() else None)
        elif args.command == "inventory":
            output = inventory(given_path(args.directory))
        elif args.command == "tools":
            output = tools(given_path(args.file))
        elif args.command == "provenance":
            output = provenance(args.checks)
        else:
            output = scan([given_path(path) for path in args.paths])
    except InputError as exc:
        print(single_line(f"FAILED {exc}"))
        return 1
    except TreeRefused as exc:
        print(single_line(f"FAILED {refused_text(exc)}"))
        return 1
    except FileRefused as exc:
        print(single_line(f"FAILED cannot read {posix(exc.path)}: {exc.reason}"))
        return 1
    except OSError as exc:
        where = f" {Path(exc.filename).as_posix()}" if exc.filename else ""
        print(single_line(f"FAILED cannot read{where}: {exc.strerror or exc}"))
        return 1
    # A line names paths, names, and values the audited files give; none of them may end the line it sits on.
    print("\n".join(single_line(line) for line in output))
    return 1 if any(line.startswith(("STOP ", "UNREADABLE ", "NOT_A_CHECK ")) for line in output) else 0


if __name__ == "__main__":
    run_main(main)
