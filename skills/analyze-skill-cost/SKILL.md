---
name: analyze-skill-cost
description: "Audit an agent skill for cost and efficiency: token footprint, tool calls, deterministic work left to prose, subagent overhead, runtime adapters, model, and allowed-tools. Read-only findings report. Use it when asked what a skill costs, why it is slow or expensive, or to check one before shipping it."
argument-hint: "<SkillName>"
allowed-tools: ["Bash(python -B \"${CLAUDE_SKILL_DIR}/scripts/*)", "PowerShell(python -B \"${CLAUDE_SKILL_DIR}/scripts/*)", "Read", "Grep"]
---

Audit a named agent skill for cost and efficiency regressions and render a bucketed findings report. Read-only — no edits, no prompts.

The measurements are commands of `skill_inventory.py`, run exactly as shown. Each prints one fact per line, with paths in forward-slash form; a missing input prints `FAILED <reason>` on stderr and exits 2. Do not re-measure, re-count, or write your own code for anything a command reports. Your job is the judgment the commands cannot make.

## Step 0 — Parse `$ARGUMENTS`

Trim leading/trailing whitespace. If empty, print `usage: analyze-skill-cost <SkillName>` and stop. Otherwise `SKILL_NAME=$ARGUMENTS`. No flags.

## Step 1 — Locate the skill

```bash
python -B "${CLAUDE_SKILL_DIR}/scripts/skill_inventory.py" locate "<SKILL_NAME>"
```

It searches `{{HOME}}/.claude/skills/`, the current repository's `.agents/skills/` and `.claude/skills/`, and, in a skill source tree, `skills/` (`SCOPE source`, preferred over the deployed copy).

- `SKILL_FILE <path>`, `SKILL_DIR <path>`, `SCOPE <scope>`: continue with these.
- `NOT_FOUND <name>` followed by `AVAILABLE <scope> <name>` lines: stop with `skill '<SKILL_NAME>' not found. Available skills: <user: …> / <project: …>`.
- `AMBIGUOUS <scope> <path>` lines: stop with `ambiguous skill name '<SKILL_NAME>' — found at: <paths>. Rename one or delete the duplicate.`

## Step 2 — Inventory and token estimate

```bash
python -B "${CLAUDE_SKILL_DIR}/scripts/skill_inventory.py" inventory "<SKILL_DIR>"
```

`FILE` lines list every file by category; a `doc` file is Markdown other than the main `SKILL.md`. `TOTAL` lines fill the report's Scope, and Step 4 judges the rest.

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

Pass the main file followed by every `doc` path:

```bash
python -B "${CLAUDE_SKILL_DIR}/scripts/skill_inventory.py" scan "<SKILL_FILE>" "<doc path>"
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
- **SUGGESTION** — the agent must parse or normalize command output, or loop applying rule-based decisions (counting, comparing, sorting, classifying by fixed rules). Cues: `rule-based-work`, `loop`.

Recommend a tested command under `scripts/` that prints one fact per line, so the agent keeps only judgment and user questions. Respect the limits: prefer an established tool or an existing option (a `--json` output flag, `gh --jq`) over a new script; never script genuine judgment such as severity, wording, or whether a section earns its space; and skip the recommendation when the step is rare or a one-liner and the script's tests and upkeep would outweigh the tokens and calls it saves.

## Step 6 — Agent delegation cost

Review every `agent` cue. For each subagent invocation:

- Identify the subagent type, if named. Estimate the prompt size from the surrounding quoted block or described context injection.
- Two independent subagent calls described sequentially → **MUST FIX**; recommend parallel fan-out in a single assistant turn. A parallel fan-out in one turn is good; never flag it.
- Injected context that could be passed as a file path instead (e.g. an inlined file list) → SUGGESTION.
- A scope narrow enough for an inline search or read → SUGGESTION.
- `subagent-reply` (the skill delegates but never bounds the reply) → SUGGESTION: even when the result goes to a file, every closing message lands in the orchestrator's context. Recommend a fixed one-line reply such as `WROTE <path>`.
- Each `RUNTIME_PROMPT <file> <line> <script>`: a subagent reads a prompt that the named script (or `unknown`) writes at runtime, so this audit cannot see it. Never judge it as a small prompt; report a SUGGESTION to audit what that script renders: its size, any chain of reads it starts, and whether it bounds the reply.
- A subagent call inside a loop over N items with no cap → **MUST FIX** (unbounded fan-out).

## Step 7 — Model, allowed-tools, and listing

```bash
python -B "${CLAUDE_SKILL_DIR}/scripts/skill_inventory.py" tools "<SKILL_FILE>"
```

`MODEL none` means the caller's model applies. A tool is `USED` only where the body names it in a tool-use context (a code span, call syntax, "tool" or "call", or a shell fence); a sentence that starts with "Read" is not a use. `IMPLIED` means prose names the tool's action ("read", "search", "ask", "subagent") without naming the tool. The bullets below say what each other line means for the report.

Model selection. Claude adapter values are `haiku`, `sonnet`, `opus`, or absent. For other runtimes, also inspect any native adapter metadata when present.

- **SUGGESTION** — a Claude skill does purely mechanical work (all shell and file/search operations; no synthesis, no subagent calls requiring reasoning, no natural-language output beyond a fixed template) and has no model set or pins `sonnet`/`opus`. Suggest trying `model: haiku`, and only after comparing real runs before and after: a cheaper model can quietly lower quality, so never make it a MUST FIX or recommend it without that comparison. Also warn that in Claude Code a skill's `model` applies for the rest of the turn that invoked it, not only while the skill runs; the session's model resumes at the user's next prompt. Every subagent started later in that turn without an explicit model runs on the skill's model. Never suggest a cheaper `model` for a skill that is usually followed, in the same turn, by work that starts subagents. When such a skill can run without the conversation's context, suggest `context: fork` with `model` instead, since `model` then sets only the forked subagent's model. For another runtime, suggest its low-cost equivalent only when that runtime supports skill-level model selection, under the same condition.
- **SUGGESTION** — a skill pins `haiku` but includes reasoning-heavy steps (multi-file synthesis, severity judgment, written recommendations). Recommend removing the pin so the caller's default applies.

Listing. While `INVOCATION` is `model`, every session in every project loads the description, whether or not the skill runs.

- `INTERNAL_LISTED` → **MUST FIX**: add `disable-model-invocation: true`, and `user-invocable: false` when users should not start it either.
- A skill only the user should start (it deletes, deploys, or otherwise acts outside the conversation) with `INVOCATION model` → SUGGESTION: add `disable-model-invocation: true`. First check that no other skill invokes it by name, since that needs model invocation.
- A long description → SUGGESTION to trim it to what tells the model when to use the skill. Judge by content, not a fixed length.

Allowed-tools grants. In Claude Code, `allowed-tools` pre-approves its entries for the turn that starts the skill and restricts nothing, so judge each entry by what it lets run unasked. A shell pattern matches the command's text, quotes included, with `*` for any text; on Windows the model may run a fence through Bash or PowerShell.

- `UNUSED_ALLOWED <tool>` → SUGGESTION "remove from `allowed-tools`", after confirming the body does not need it in words the heuristic misses (invoking another skill by name needs skill invocation).
- `MISSING_ALLOWED <tool> <line>` → for Claude, **MUST FIX** when that line tells the agent to use the tool, since the skill then prompts mid-run; ignore a line that only mentions it, such as a rubric or a counter-example. For another runtime, check that the compatibility contract provides a usable first-class mapping instead of requiring Claude tool names in native metadata.
- `UNSCOPED_ALLOWED <entry>` → **MUST FIX**: it pre-approves every shell command. Recommend patterns for the commands the fences run, written as they are, such as `Bash(python -B "${CLAUDE_SKILL_DIR}/scripts/*)` with the same `PowerShell(...)` pattern.
- `UNPAIRED_ALLOWED <entry>` → **MUST FIX** for a skill that runs on Windows, SUGGESTION otherwise: add the other shell's identical pattern, or the user is prompted, or denied in a non-interactive run, whenever the model picks that shell.
- `UNGRANTED <tool> <line> <command>` → **MUST FIX** when the command runs the skill's own or a sibling's scripts; correct the pattern rather than widen it. For any other command, SUGGESTION to grant it exactly as written, unless it runs code from the target repository or acts outside the conversation, which should keep prompting.
- `EXPANDS <line> <command>` → **MUST FIX** for a command the skill tells the agent to run: Claude Code asks before any command that expands a shell variable or `$(...)`, whatever is granted. `${CLAUDE_SKILL_DIR}` and `$ARGUMENTS` are filled in first, so they never count. Recommend that the script take the value as its default, such as the current directory.

## Step 8 — Bucket and render

Group all findings into four buckets. Within each bucket, subdivide into MUST FIX and SUGGESTIONS; omit empty subsections. A bucket with zero findings renders as `No issues found. ✓` (omit the sub-headings). No `SHOULD FIX` tier — promote to MUST FIX.

1. **Token footprint** — Step 4
2. **Tool-call efficiency** — Step 5, including deterministic steps left to prose
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
- Frontmatter model: <value or "(inherits caller)">
- Listing: <INVOCATION>, description ≈ <N> tok loaded every session while model-invocable
- Allowed tools: <comma-separated list>

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
