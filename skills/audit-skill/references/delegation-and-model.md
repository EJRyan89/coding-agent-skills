# Delegation and model rules

## Delegation

Review every `agent` cue. For each subagent invocation:

- Identify the subagent type, if named. Estimate the prompt size from the surrounding quoted block or described context injection.
- Two independent subagent calls described sequentially → **MUST FIX**; recommend parallel fan-out in a single assistant turn. A parallel fan-out in one turn is good; never flag it.
- Injected context that could be passed as a file path instead (e.g. an inlined file list) → SUGGESTION.
- A scope narrow enough for an inline search or read → SUGGESTION.
- `subagent-reply` (the skill delegates but never bounds the reply) → SUGGESTION: even when the result goes to a file, every closing message lands in the orchestrator's context. Recommend a fixed one-line reply such as `WROTE <path>`.
- Each `RUNTIME_PROMPT <file> <line> <script>`: a subagent reads a prompt that the named script (or `unknown`) writes at runtime, which the skill's own files never show. The lines after it measure the rendered sample of that prompt, kept in the skill's source tree:
  - `RUNTIME_PROMPT_SAMPLE <sample> <tokens>`, the rendered prompt's estimate; `RUNTIME_PROMPT_READ <sample> <path> <tokens>`, each skill file it tells the subagent to read on every run; and `REPLY_BOUNDED <sample> yes|no`. Report each sample in the Scope. Judge the sample as you judge a body, by content: a SUGGESTION only for a prompt that restates its reads or carries text the subagent never acts on, a read whose content the prompt could carry in a line, or a read that sends the subagent on to further files. `REPLY_BOUNDED <sample> no` → SUGGESTION to end the prompt with a fixed one-line reply such as `WROTE <path>`. Otherwise the runtime prompt has no finding.
  - `RUNTIME_PROMPT_UNSAMPLED <file> <line> <reason>`: no sample was found, for the reason given. Never judge the prompt as a small one; report a SUGGESTION to audit what that script renders by hand (its size, any chain of reads it starts, and whether it bounds the reply), naming the reason, and to keep a rendered sample beside the source.
- A subagent call inside a loop over N items with no cap → **MUST FIX** (unbounded fan-out).

## Model

Claude adapter values are `haiku`, `sonnet`, `opus`, or absent. For other runtimes, also inspect any native adapter metadata when present.

- **SUGGESTION** — a Claude skill does purely mechanical work (all shell and file/search operations; no synthesis, no subagent calls requiring reasoning, no natural-language output beyond a fixed template) and has no model set or pins `sonnet`/`opus`. Suggest trying `model: haiku`, and only after comparing real runs before and after: a cheaper model can quietly lower quality, so never make it a MUST FIX or recommend it without that comparison. Also warn that in Claude Code a skill's `model` applies for the rest of the turn that invoked it, not only while the skill runs; the session's model resumes at the user's next prompt. Every subagent started later in that turn without an explicit model runs on the skill's model. Never suggest a cheaper `model` for a skill that is usually followed, in the same turn, by work that starts subagents. When such a skill can run without the conversation's context, suggest `context: fork` with `model` instead, since `model` then sets only the forked subagent's model. For another runtime, suggest its low-cost equivalent only when that runtime supports skill-level model selection, under the same condition.
- **SUGGESTION** — a skill pins `haiku` but includes reasoning-heavy steps (multi-file synthesis, severity judgment, written recommendations). Recommend removing the pin so the caller's default applies.
