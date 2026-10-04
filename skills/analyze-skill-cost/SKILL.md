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

It searches `{{HOME}}/.claude/skills/<SKILL_NAME>/` and, when the current directory is in a Git repository, that repository's `.agents/skills/` and `.claude/skills/`, accepting `SKILL.md` or `skill.md`. In a skill source tree, where `skills/<SKILL_NAME>/` has a `deploy-meta/<SKILL_NAME>.json`, it also finds the source (`SCOPE source`) and prefers it over the deployed user copy. A file reached through two roots (a symlink or the same directory) counts once. `REPO <path>` or `REPO none` says which repository it searched.

- `SKILL_FILE <path>`, `SKILL_DIR <path>`, `SCOPE <scope>`: continue with these.
- `NOT_FOUND <name>` followed by `AVAILABLE <scope> <name>` lines: stop with `skill '<SKILL_NAME>' not found. Available skills: <user: …> / <project: …>`.
- `AMBIGUOUS <scope> <path>` lines: stop with `ambiguous skill name '<SKILL_NAME>' — found at: <paths>. Rename one or delete the duplicate.`

## Step 2 — Inventory and token estimate

```bash
python -B "${CLAUDE_SKILL_DIR}/scripts/skill_inventory.py" inventory "<SKILL_DIR>"
```

- `FILE <category> <bytes> <est_tokens> <path>` for every file except `.git`, `__pycache__`, and `node_modules` contents. Categories: `main` is the root `SKILL.md`; `helper` is a `.sh`, `.bash`, `.ps1`, `.py`, `.js`, `.mjs`, `.cjs`, or `.ts` script; `doc` is any other Markdown; `data` is everything else.
- `TOTAL <category> <files> <bytes> <est_tokens>` for each category, then `TOTAL all`.
- Flags: `BODY_OVER_8KB <bytes>` (main over 8 KiB), `DOC_OVER_4KB <bytes> <path>`, `DUPLICATE_BLOCK <path:line> <first path:line>` (identical fenced blocks), `INLINED_HELPER <path:line> <helper>` (a fenced block copied from a helper script), and `NO_MAIN`.
- `OUTSIDE_READ <path:line> <bytes> <est_tokens> <file>` for each Markdown file outside the skill folder that its Markdown names (a `../` path to a Markdown file, also after the skill-directory variable), `OUTSIDE_MISSING <path:line> <file>` when that file does not exist, and `TOTAL outside <files> <bytes> <est_tokens>`, counting each file once. From the skill's source tree, `DECLARED shared <asset>` and `DECLARED skill <name>` list its `deploy-meta` dependencies.

The estimate is `ceil(prose characters / 4 + code characters / 3)`. Helper and data files are all code; in Markdown, lines inside fenced blocks are code and the rest is prose; a binary file counts 0. It is a relative signal, not the active model's tokenizer.

## Step 3 — Read what the judgment needs

Read the main body and every `doc` file in full; they are what the audit judges. Read a helper script only when a finding depends on it, for example to check whether the body has the agent redo logic the script already implements. Helpers that run through a shell do not load into context: report their size as reference, not as context cost.

## Step 4 — Token footprint

- **MUST FIX** — `BODY_OVER_8KB` **and** the body contains duplicated code blocks (`DUPLICATE_BLOCK`), repeated "Important" prose that restates step bodies, or trimmable example output. Length alone is not a MUST FIX: a 20 KB skill whose every section earns its space passes, and a short skill full of duplicated prose does not.
- **SUGGESTION** — a `DOC_OVER_4KB` file whose content the body copies inline instead of pointing at it. Recommend an instruction to read the file.
- **SUGGESTION** — each `OUTSIDE_READ` file: it costs a turn and its tokens on every run. Recommend keeping only the rules the skill needs in the skill; runtime guidance such as a tool mapping belongs in the runtime adapters. A `DECLARED` dependency the Markdown never names is not a per-run cost.
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

Recommend a tested command under `scripts/` that prints one fact per line, so the agent keeps only judgment and user questions. Respect the limits: prefer an established tool or an existing option (a `--json` output flag, `gh --jq`) over a new script; never script genuine judgment such as severity, wording, or whether a section earns its space; and skip the recommendation when the step is rare or a one-liner and the script's tests and upkeep would outweigh the tokens and calls it saves. These findings belong to the Tool-call efficiency bucket.

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

It prints `MODEL <value>` (`none` when absent, so the caller's model applies), `DESCRIPTION <chars> <est_tokens>`, `INVOCATION model|user-only|hidden`, `INTERNAL_LISTED` when the description calls the skill internal but the model can still invoke it, `ALLOWED <tool>` lines or `NO_ALLOWED_TOOLS`, `USED <tool> <line>`, `IMPLIED <tool> <line>`, `UNUSED_ALLOWED <tool>`, `MISSING_ALLOWED <tool> <line>`, `UNSCOPED_ALLOWED <entry>`, `UNPAIRED_ALLOWED <entry>`, `UNGRANTED <tool> <line> <command>`, and `EXPANDS <line> <command>`. A tool is `USED` only where the body names it in a tool-use context: at the start of a code span, in call syntax, followed by "tool" or "call", as a multi-word tool name that is not an English word, or through a shell fence (each of Bash and PowerShell that is allowed, else Bash). A sentence that starts with "Read" or says "read and apply" is not a use. `IMPLIED` means prose names the tool's action ("read", "search", "ask", "subagent") without naming the tool.

Model selection. Claude adapter values are `haiku`, `sonnet`, `opus`, or absent. For other runtimes, also inspect any native adapter metadata when present.

- **SUGGESTION** — a Claude skill does purely mechanical work (all shell and file/search operations; no synthesis, no subagent calls requiring reasoning, no natural-language output beyond a fixed template) and has no model set or pins `sonnet`/`opus`. Suggest trying `model: haiku`, and only after comparing real runs before and after: a cheaper model can quietly lower quality, so never make it a MUST FIX or recommend it without that comparison. Also warn that in Claude Code a skill's `model` applies for the rest of the turn that invoked it, not only while the skill runs; the session's model resumes at the user's next prompt. Every subagent started later in that turn without an explicit model runs on the skill's model: a code review started in the same turn as a Haiku-pinned skill ran every reviewer on Haiku, and they missed a must-fix finding. Never suggest a cheaper `model` for a skill that is usually followed, in the same turn, by work that starts subagents. When such a skill can run without the conversation's context, suggest `context: fork` with `model` instead, since `model` then sets only the forked subagent's model. For another runtime, suggest its low-cost equivalent only when that runtime supports skill-level model selection, under the same condition.
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

The skill is read-only: no file edits, no user prompts, and no Git operations beyond the repository lookup `locate` performs.
