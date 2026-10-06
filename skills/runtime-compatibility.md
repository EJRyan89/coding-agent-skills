# Runtime compatibility

Claude Code is the authoritative source for these user-level skills and runs them directly, without this file. Other agent runtimes load the same files through the generated adapters in `{{HOME}}/.agents/skills`, which point here first. Apply this contract before executing any skill; it overrides runtime-specific tool or model wording in an individual skill.

## Tool mapping

- Use the runtime's first-class shell, file, search, editing, user-input, and delegation capabilities.
- Treat `Bash`, `Read`, `Write`, `Edit`, `Grep`, `Glob`, `AskUserQuestion`, `Agent`, `Skill`, and `Workflow` as Claude adapter names. On another runtime, use the closest first-class equivalent.
- `Workflow` is Claude Code's scripted multi-agent tool and has no equivalent elsewhere. A skill that offers a Workflow path also gives a path without it; take that one.
- If structured user input is unavailable, ask the question directly and pause for the answer.
- If the active shell is not Bash, run non-trivial Bash blocks with Git Bash. Translate only simple commands when semantics remain unchanged.
- For `Agent`, use native subagent delegation. Map a named specialist to the matching native role when available; otherwise delegate to a general worker and include the specialist instructions in its prompt.
- For `Skill`, use native skill invocation. If the runtime has discovery but no invocation tool, load and apply the selected skill's authoritative instructions directly.
- Run skill-provided script invocations exactly as shown, with the same arguments. Do not reimplement their logic inline.
- A skill's `allowed-tools` list is Claude Code pre-approval. The adapters do not carry it, so ask for approval as this runtime normally does.

## Paths

- Paths under `{{HOME}}/.claude` are intentionally canonical because Claude is authoritative; they are not portability defects.
- `${CLAUDE_SKILL_DIR}` in a skill is that skill's own directory, which its adapter states, not the adapter's directory or the working directory. Substitute it before running a command or reading a file. In a skill file read directly rather than through an adapter, it is the directory containing that `SKILL.md`.
- Respect the active runtime's sandbox and approval model for all user-profile, network, and repository writes.
