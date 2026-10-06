---
name: audit-ai-config
description: "Read-only assessment of a repository's AI agent configuration for Claude Code, Codex, and Copilot that reports findings and never writes files. Use it when asked to audit or check a repository's agent instructions or setup, not to create or change them."
allowed-tools: ["Bash(python -B \"${CLAUDE_SKILL_DIR}/scripts/*)", "PowerShell(python -B \"${CLAUDE_SKILL_DIR}/scripts/*)", "Bash(git rev-parse --show-toplevel)", "PowerShell(git rev-parse --show-toplevel)", "Read", "Glob", "AskUserQuestion"]
---

# Audit AI Agent Configuration

Assess a repository's AI agent configuration across Claude Code, Codex, and Copilot and report findings by severity. The audit engine performs every deterministic check; you run it, relay its report, and add only the judgment it cannot make. Never write, modify, or delete files, and do not read the engine to redo its checks by hand. Remediation belongs to `init-ai-config` or manual fixes.

## 1. Run the engine

Confirm the target is a Git repository (`git rev-parse --show-toplevel`); if it is not, ask the user which repository to audit. Then run, from anywhere:

```bash
python -B "${CLAUDE_SKILL_DIR}/scripts/audit_ai_config.py" --root "<repository root>"
```

Add `--json` when the user wants machine-readable output.

| Exit | Meaning | Report as |
|---|---|---|
| `0` | No `ERROR` findings and the authority is conforming | Compliant within statically verifiable scope |
| `1` | One or more `ERROR` findings | Errors found |
| `2` | Authority is ambiguous, unconfigured, or an alternative source (`INCONCLUSIVE`) | Inconclusive; the conforming-only checks did not run |

Relay the report's authority and scope status, every `ERROR` and `WARNING` finding, and every finding whose check is `limitation`, which names a documented limitation that applies to this repository. For the remaining `INFO` findings, relay only the engine's `SUMMARY` lines, which count findings per severity and `INFO` findings per check; give the full `INFO` list only when the user asks. `${CLAUDE_SKILL_DIR}/references/audit-policy.md` explains what every check covers, `${CLAUDE_SKILL_DIR}/references/report-schema.md` the finding format, and `${CLAUDE_SKILL_DIR}/references/known-limitations.md` the limitations in full; read them only to answer a question the report raises.

## 2. Review instruction content (judgment)

Only for a conforming repository, read the instruction files the engine lists as effective sources for each targeted surface (the `layering` findings) and every file the glob `.github/instructions/**/*.instructions.md` matches, and report as `WARNING` findings of your own:

- instructions that contradict each other within one surface's effective set, such as two sources prescribing different build commands or formatting rules;
- substantial overlap that duplicates `CLAUDE.md` guidance in a non-generated file, which will drift;
- a missing dogfooding requirement, when the repository ships tools or rules that it should also apply to itself.

Label these as your assessment, separate from the engine's findings, and do not change the engine's exit status because of them.

## 3. Optional operational validation

Only when the user explicitly authorizes it for a trusted repository, because both commands execute repository-controlled code:

1. Run the repository's generator in read-only mode, never `--write`: `python -B .github/scripts/ai_config.py --check` from the repository root.
2. Handshake each stdio MCP server, which starts it, sends `initialize` and `tools/list`, and stops it without invoking a tool:

   ```bash
   python -B "${CLAUDE_SKILL_DIR}/scripts/mcp_handshake.py" --root "<repository root>"
   ```

   It prints `HANDSHAKE_OK`, `HANDSHAKE_FAILED <reason>`, or `SKIPPED` (remote transports) per server, or `NO_SERVERS`. `CONFIG_ERROR <file> <reason>` means that file could not be read, so its servers were not checked: report it as a failure, never as "no servers". Pass `--server <name>` to start only one.

Report the results as additional findings.
