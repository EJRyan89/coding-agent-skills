---
name: analyze-skill-cost
description: "Audit an agent skill for cost and efficiency: token footprint, tool calls, deterministic work left to prose, subagent overhead, runtime adapters, model, and allowed-tools. Read-only findings report. Use it when asked what a skill costs, why it is slow or expensive, or to check one before shipping it."
argument-hint: "<SkillName>"
allowed-tools: ["Bash({{PYTHON}} -B \"${CLAUDE_SKILL_DIR}/scripts/skill_inventory.py\" *)", "PowerShell({{PYTHON}} -B \"${CLAUDE_SKILL_DIR}/scripts/skill_inventory.py\" *)", "Read", "Grep"]
---

Audit a named agent skill for cost and efficiency regressions and render a bucketed findings report. Read-only — no edits, no prompts.

The measurements are commands of `skill_inventory.py`, run exactly as shown. Each prints one fact per line, with paths in forward-slash form; a missing or unreadable input prints `FAILED <reason>` as its last line; stop and report it. `inventory` is the exception for a Markdown file that is not UTF-8 text: it prints an `UNREADABLE` line for it, measures the rest, and exits 1, as Step 2 says. Do not re-measure, re-count, or write your own code for anything a command reports. Your job is the judgment the commands cannot make.

## Step 1 — Locate the skill

`<SKILL_NAME>` is `$ARGUMENTS` as given, even when empty; the skill takes no flags.

```bash
{{PYTHON}} -B "${CLAUDE_SKILL_DIR}/scripts/skill_inventory.py" locate "<SKILL_NAME>"
```

It searches `.claude/skills/` in your home folder, the current repository's `.agents/skills/` and `.claude/skills/`, and, in a skill source tree, `skills/` (`SCOPE source`, preferred over the deployed copy).

- `REPO <path>` or `REPO none`: the repository searched; nothing to do.
- `SKILL_FILE <path>`, `SKILL_DIR <path>`, `SCOPE <scope>`: continue with these.
- `STOP <message>`: show the message and stop. It ends the `NOT_FOUND <name>` and `AVAILABLE <scope> <name>` lines of a skill not found and the `AMBIGUOUS <scope> <path>` lines of one found twice, and is the only line for an empty name.

## Step 2 — Inventory and token estimate

```bash
{{PYTHON}} -B "${CLAUDE_SKILL_DIR}/scripts/skill_inventory.py" inventory "<SKILL_DIR>"
```

`FILE` lines list every file by category; a `doc` file is Markdown other than the main `SKILL.md`. `TOTAL` lines fill the report's Scope, and so do `DECLARED <kind> <name>` lines, or `DECLARED_UNREADABLE <path>` when the skill's metadata is not valid JSON. `NO_MAIN` means the folder lost its `SKILL.md`, and `UNREADABLE <path> <reason>` that a Markdown file it loads cannot be measured: stop and report either. Step 4 judges the rest.

## Step 3 — Read what the judgment needs

Read the main body and every `doc` file in full; they are what the audit judges. Read a helper script only when a finding depends on it, for example to check whether the body has the agent redo logic the script already implements. Helpers that run through a shell do not load into context: report their size as reference, not as context cost.

## Step 4 — Token footprint

Judge content, not size: a long body whose every section earns its space passes, and a short one full of duplicated prose does not.

- **MUST FIX** — the body contains duplicated code blocks (`DUPLICATE_BLOCK`), prose that restates another step's instruction (such as "Important" bullets repeating step bodies), or trimmable example output. Documentation the agent never acts on, such as safety guarantees the scripts enforce, a glossary of self-labelled output, or a procedure for an action the skill never takes, counts as restatement: recommend moving it to the user documentation.
- **SUGGESTION** — a `doc` file whose content the body copies inline instead of pointing at it. Recommend an instruction to read the file.
- **SUGGESTION** — the structure checks from Anthropic's skill authoring best practices: `BODY_OVER_500_LINES` (split the body into reference files), `DOC_NO_TOC` (add a table of contents; skip a template the agent copies whole), and `NESTED_REFERENCE` (link that file from `SKILL.md`, since references should be one level deep).
- **SUGGESTION** — each `OUTSIDE_READ` file: it costs a turn and its tokens on every run. Recommend keeping only the rules the skill needs in the skill; runtime guidance such as a tool mapping belongs in the runtime adapters. A `DECLARED` dependency the Markdown never names is not a per-run cost.
- **MUST FIX** — each `OUTSIDE_MISSING`: the skill names a file outside its folder that does not exist, so that read fails on every run. Recommend correcting the path or the dependency.
- **SUGGESTION** — each `INLINED_HELPER`. Recommend invoking the script instead of copying it.

## Step 5 — Tool-call efficiency

It scans the main file and every `doc` file, then compares the status lines they name with the scripts':

```bash
{{PYTHON}} -B "${CLAUDE_SKILL_DIR}/scripts/skill_inventory.py" scan "<SKILL_DIR>"
```

Each `CUE <file> <line> <cue> <text>` is a candidate, not a finding. Read it in context and keep only real findings, anchored as `file:line`. Frontmatter is skipped, and only `wide-glob`, `generated-code`, and `shell-file-command` apply inside fenced blocks.

| Pattern | Cue | Severity | Recommendation |
|---|---|---|---|
| Multiple shell steps described sequentially with no data dependency | — | SUGGESTION | "Issue these in a single assistant turn as parallel tool calls" |
| "Read <file> and then search it for X" | `read-then-search` | SUGGESTION | "Search directly — avoids loading the full file into context" |
| Loop over files with a per-file read | `loop` | SUGGESTION | "Batch with one multi-file search (`-l`, or `-A`/`-B` context)" |
| Search over wide globs (`**/*.cs`, `**/*`) with no `head_limit` | `wide-glob` | MUST FIX | "Add `head_limit` — unbounded output on large repos wastes tokens" |
| `cat`/`head`/`tail`/`find`/`grep`/`rg` in step descriptions rather than scripts | `shell-file-command` | SUGGESTION | "Use the runtime's first-class file/search tools instead of shell calls" |
| The same command run once per item | `per-item-command` | SUGGESTION | "Let the command take several items, so one call covers the batch" |
| Script output shown to the user as-is or verbatim | `relayed-output` | SUGGESTION | "Write the report to a file and print its path; relayed output is re-typed as output tokens" (skip when the run is rare and the output small) |

### Deterministic steps left to prose

Flag steps that make the agent do work a program would do the same way every time:

- **MUST FIX** — the agent must write or generate code to finish a step (glue scripts, `python -c`, heredoc programs). Cue: `generated-code`.
- **MUST FIX** — the body has the agent redo logic that a script in the skill or one of its dependencies already implements, such as reading a module to learn its functions and then calling them by hand.
- **SUGGESTION** — the agent must parse or normalize command output, or loop applying rule-based decisions (counting, comparing, sorting, classifying by fixed rules). Cues: `rule-based-work`, `loop`, and the shapes below, each with what to recommend.

| Prose that | Cue | Recommendation |
|---|---|---|
| Chooses a command's flags from the lines or values another command printed | `assembled-arguments` | "Have the first command print the second one's arguments on one line, passed as printed" |
| Splits, groups, or renames output by a fixed rule, or takes a printed value apart | `parsed-output` | "Have the script take the value as printed, or print the parts, groups, or status itself" |
| Carries a count, a summary, or a last-seen value from step to step | `carried-state` | "Keep the record in a file and add a command that prints the totals" |
| Decides the regenerations, gates, or follow-ups a change owes by listing rules | `owed-by-rules` | "Add a command that reads the change and prints one line per thing it owes" |
| Drafts a file or a message by hand from a template, or copies a list of printed lines into one | `hand-drafted` | "Have the script draft the file, or print the block to paste as it is" |

Recommend a tested command under `scripts/` that prints one fact per line, so the agent keeps only judgment and user questions. Respect the limits: prefer an established tool or an existing option (a `--json` output flag, `gh --jq`) over a new script; never script genuine judgment such as severity, wording, or whether a section earns its space; and skip the recommendation when the step is rare or a one-liner and the script's tests and upkeep would outweigh the tokens and calls it saves.

### Status lines the scripts print

The same command compares the status words the skill's Markdown names with what its scripts print and exit with; the script's docstring says which scripts it reads.

- `UNPRINTED <file> <line> <word>` → **MUST FIX** when the word names a line the skill acts on: no script prints it, so the agent waits for a line that never comes. Recommend the word a script prints, or the script change. Drop it when the word names another program's line, a variable, or a value.
- `EXIT_MISMATCH <file> <line> <code>` → **MUST FIX**: the prose states an exit code no script uses. Recommend the code a script exits with, or acting on the printed line instead.
- `UNEXPLAINED <script> <line> <word>` → SUGGESTION: the agent meets a line it was not told about. Recommend naming it beside the command that prints it; drop it for a command the skill never runs, or one whose lines the body sends the agent to a list of.

## Step 6 — Agent delegation cost

When Step 5 printed an `agent` or `subagent-reply` cue or a `RUNTIME_PROMPT` line, read `${CLAUDE_SKILL_DIR}/references/delegation-and-model.md` and judge them by its Delegation section; it also says what the runtime prompt lines that follow mean. Otherwise the skill delegates nothing and this bucket has no findings.

## Step 7 — Model, allowed-tools, and listing

```bash
{{PYTHON}} -B "${CLAUDE_SKILL_DIR}/scripts/skill_inventory.py" tools "<SKILL_FILE>"
```

`MODEL none` means the caller's model applies. A tool is `USED` only where the body names it in a tool-use context (a code span, call syntax, "tool" or "call", or a shell fence); a sentence that starts with "Read" is not a use. `IMPLIED` means prose names the tool's action ("read", "search", "ask", "subagent") without naming the tool; a prohibition ("do not read") and a prompt quoted for a subagent never count. The bullets below say what each other line means for the report.

Model. When `MODEL` names a model, or the skill's work is purely mechanical (shell and file operations only, no synthesis, no output beyond a fixed template), judge it by the Model section of `${CLAUDE_SKILL_DIR}/references/delegation-and-model.md`. Otherwise the model has no finding.

Listing. While `INVOCATION` is `model`, every session in every project loads the description that `DESCRIPTION <characters> <tokens>` measures, whether or not the skill runs.

- `INTERNAL_LISTED` → **MUST FIX**: add `disable-model-invocation: true`, and `user-invocable: false` when users should not start it either.
- A skill only the user should start (it deletes, deploys, or otherwise acts outside the conversation) with `INVOCATION model` → SUGGESTION: add `disable-model-invocation: true`. First check that no other skill invokes it by name, since that needs model invocation.
- A long description → SUGGESTION to trim it to what tells the model when to use the skill. Judge by content, not a fixed length.

Allowed-tools grants. `ALLOWED` lines fill the Scope. `NO_ALLOWED_TOOLS` renders it as `(none)`, and each `USED` line then counts as a `MISSING_ALLOWED` line. In Claude Code, `allowed-tools` pre-approves its entries for the turn that starts the skill and restricts nothing, so judge each entry by what it lets run unasked. A shell pattern matches the command's text, quotes included, with `*` for any text; on Windows the model may run a fence through Bash or PowerShell.

- `UNUSED_ALLOWED <tool>` → SUGGESTION "remove from `allowed-tools`", after confirming the body does not need it in words the heuristic misses (invoking another skill by name needs skill invocation).
- `MISSING_ALLOWED <tool> <line>` → for Claude, **MUST FIX** when that line tells the agent to use the tool, since the skill then prompts mid-run; ignore a line that only mentions it, such as a rubric or a counter-example. For another runtime, check that the compatibility contract provides a usable first-class mapping instead of requiring Claude tool names in native metadata.
- `UNSCOPED_ALLOWED <entry>` → **MUST FIX**: it pre-approves every shell command. Recommend patterns for the commands the fences run, written as they are, each script by name, the pattern ending at the script's closing quote, a space, and `*`, with the same `PowerShell(...)` pattern.
- `UNPAIRED_ALLOWED <entry>` → **MUST FIX** for a skill that runs on Windows, SUGGESTION otherwise: add the other shell's identical pattern, or the user is prompted, or denied in a non-interactive run, whenever the model picks that shell.
- `SIBLING_FOLDER_ALLOWED <entry>` → **MUST FIX**: the pattern reaches a sibling skill's folder rather than one script, so it matches every script there and every `..` path out of it. Grant each sibling script the body runs by name, the pattern ending at the script's closing quote, a space, and `*`.
- `OPEN_PATH_ALLOWED <entry>` → **MUST FIX**: the pattern's `*` continues a path, as a folder's `/*` or `tool.py*` does, so it matches every file that path starts and every `..` path out of it, which reaches every installed skill's scripts. Grant each script the body runs by name, the pattern ending at the script's closing quote, or at a space when the path is unquoted, then `*`.
- `NO_SCRIPT_ALLOWED <entry>` → **MUST FIX**: the pattern's text before its `*` names a program but no script, such as `{{PYTHON}} *` or `bash -c *`, or `git` or `gh` with no subcommand, so it pre-approves any code or subcommand the `*` supplies. Grant each script the body runs by name, or the `git` or `gh` subcommand it runs.
- `UNGRANTED <tool> <line> <command>` → **MUST FIX** when the command runs the skill's own or a sibling's scripts; correct the pattern rather than widen it. For any other command, SUGGESTION to grant it exactly as written, unless it runs code from the target repository or acts outside the conversation, which should keep prompting.
- `IDLE_ALLOWED <entry>` → **MUST FIX**: the pattern matches no command in a shell fence or code span, so it pre-approves a command the skill never asks for, such as one a repository leaves to prompt. Remove it, or show the command it is for.
- `HIDDEN_ALLOWED` → **MUST FIX**: no turn starts a hidden skill, so its grants approve nothing. Remove `allowed-tools`; the skills that run its scripts grant them.
- `GRANTED_REPOSITORY_CODE <tool> <line> <command>` → **MUST FIX**: a grant pre-approves a script that declares `RUNS_REPOSITORY_CODE` because it starts code from the target repository. Narrow the pattern to name the skill's other scripts. `REPOSITORY_CODE` is that command left to prompt, as it should, and has no finding.
- `EXPANDS <line> <command>` → **MUST FIX** for a command the skill tells the agent to run: Claude Code asks before any command that expands a shell variable or `$(...)`, whatever is granted. `${CLAUDE_SKILL_DIR}` and `$ARGUMENTS` are filled in first, so they never count. Recommend that the script take the value as its default, such as the current directory.

## Step 8 — Bucket and render

Group all findings into four buckets. Within each bucket, subdivide into MUST FIX and SUGGESTIONS; omit empty subsections. A bucket with zero findings renders as `No issues found. ✓` (omit the sub-headings). No `SHOULD FIX` tier — promote to MUST FIX.

1. **Token footprint** — Step 4
2. **Tool-call efficiency** — Step 5, including deterministic steps left to prose and status lines
3. **Agent delegation cost** — Step 6
4. **Model, tools & listing** — Step 7

### Report template

```markdown
# Skill Cost Audit — <SKILL_NAME>

## Scope
- Location: <SKILL_FILE>
- Inventory: main=<bytes> B, helpers=<count>/<total bytes> B, supporting docs=<count>/<total bytes> B
- Token estimate (rough; ceil(prose chars / 4 + code chars / 3), not the model's tokenizer):
  - Body ≈ <N> tok
  - Supporting docs ≈ <M> tok
  - Helpers ≈ <K> tok (invoked via a shell — not auto-loaded)
  - Outside reads ≈ <T> tok in <n> files (read on every run)
  - Runtime prompt <sample> ≈ <N> tok, its reads ≈ <M> tok in <n> files, reply bounded <yes|no> (one line per sample; omit when none)
- Frontmatter model: <value or "(inherits caller)">
- Listing: <INVOCATION>, description ≈ <N> tok loaded every session while model-invocable
- Allowed tools: <comma-separated list>
- Declared dependencies: <DECLARED names, "none", or "unreadable <path>">

---

## Token footprint
### MUST FIX
- [SKILL.md:<line>] <description> — <recommendation>
### SUGGESTIONS
- …

## Tool-call efficiency
### MUST FIX
- …
### SUGGESTIONS
- …

## Agent delegation cost
### MUST FIX
- …
### SUGGESTIONS
- …

## Model, tools & listing
### MUST FIX
- …
### SUGGESTIONS
- …

---

## Overall verdict

| Bucket                   | MUST FIX | SUGGESTIONS |
|--------------------------|---------:|------------:|
| Token footprint          |    <N>   |    <N>      |
| Tool-call efficiency     |    <N>   |    <N>      |
| Agent delegation cost    |    <N>   |    <N>      |
| Model, tools & listing   |    <N>   |    <N>      |

**Verdict:** OPTIMIZABLE if any bucket has a MUST FIX; otherwise EFFICIENT.

## Top wins
<2–4 bullets; highest-impact changes phrased as "before → after" with an estimated saving (tokens/invocation, or calls eliminated).>
```
