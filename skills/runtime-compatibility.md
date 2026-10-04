# Runtime compatibility

Claude Code is the authoritative source for these user-level skills and runs them directly, without this file. Other agent runtimes load the same files through the generated adapters in `{{HOME}}/.agents/skills`, which point here first. Apply this contract before executing any skill; it overrides runtime-specific tool or model wording in an individual skill.

## Tool mapping

- Use the runtime's first-class shell, file, search, editing, user-input, delegation, and worktree capabilities.
- Treat `Bash`, `Read`, `Write`, `Edit`, `Grep`, `Glob`, `AskUserQuestion`, `Agent`, `Skill`, `EnterWorktree`, and `Workflow` as Claude adapter names. On another runtime, use the closest first-class equivalent.
- `Workflow` is Claude Code's scripted multi-agent tool and has no equivalent elsewhere. A skill that offers a Workflow path also gives a path without it; take that one.
- If structured user input is unavailable, ask the question directly and pause for the answer.
- If the active shell is not Bash, run non-trivial Bash blocks with Git Bash. Translate only simple commands when semantics remain unchanged.
- For `EnterWorktree`, use a native worktree transition when available. Otherwise keep the session where it is and explicitly set every subsequent file or shell operation's working directory to the target worktree.
- For `Agent`, use native subagent delegation. Map a named specialist to the matching native role when available; otherwise delegate to a general worker and include the specialist instructions in its prompt.
- For `Skill`, use native skill invocation. If the runtime has discovery but no invocation tool, load and apply the selected skill's authoritative instructions directly.
- Run skill-provided script invocations with the exact argument and environment-binding semantics shown. Preserve inline `KEY=value command ...` prefixes as inline child-process environment assignments. Do not rewrite them as `export`, `source`, persistent session mutation, or reimplemented inline logic.
- A skill's `allowed-tools` list is Claude Code pre-approval. The adapters do not carry it, so ask for approval as this runtime normally does.
- Do not route work through a compatibility MCP when the runtime has an equivalent first-class capability.

## GitHub review state

- Never submit GitHub review state unless the user explicitly asks for that external action. This includes `gh pr review --approve`, `--request-changes`, or `--comment`, and any `event` field sent to `POST /pulls/{number}/reviews`.
- When the user explicitly authorizes posting inline review comments, create the review payload with a JSON serializer and omit `event` entirely; do not send `"PENDING"` or another implicit state. Pass the serialized payload with `gh api --input <file>`, never with `--field` arguments for comment arrays and never through a shell heredoc.
- Keep posted comment bodies plain text. Do not prefix them with internal finding IDs or labels such as `**[M1 — Risk]**`.

## Models and usage

- `model: haiku`, `model: sonnet`, and `model: opus` are Claude adapter settings. Claude must honor them.
- Other runtimes should inherit the current model unless they expose a clearly equivalent selection. Interpret Haiku as low-cost mechanical work, Sonnet as balanced work, and Opus as high-reasoning planning or synthesis.
- Use runtime-reported usage data when available. Never assume an agent result contains Claude's `<usage>` block. If comparable usage is unavailable, mark usage and monetary cost as unavailable rather than estimating from conversation text.

## Paths and session identity

- Paths under `{{HOME}}/.claude` are intentionally canonical because Claude is authoritative; they are not portability defects.
- `${CLAUDE_SKILL_DIR}` in a skill is that skill's own directory, which its adapter states, not the adapter's directory or the working directory. Substitute it before running a command or reading a file. In a skill file read directly rather than through an adapter, it is the directory containing that `SKILL.md`.
- Repository skills should prefer `.agents/skills` when either runtime can resolve the path. Keep `.claude` only where the file is genuinely a Claude adapter or runtime setting.
- Use `CLAUDE_SESSION_ID` under Claude. On another runtime, use a surfaced task/thread/session identifier when available; otherwise omit the identifier or write `unavailable`.
- Respect the active runtime's sandbox and approval model for all user-profile, network, and repository writes.
