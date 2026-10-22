---
name: audit-ai-config
description: "Read-only assessment of a repository's AI agent configuration for Claude Code, Codex, and Copilot that reports findings and never writes files. Use it when asked to audit or check a repository's agent instructions or setup, not to create or change them."
allowed-tools: ["Bash({{PYTHON}} -B \"${CLAUDE_SKILL_DIR}/scripts/audit_ai_config.py\" *)", "PowerShell({{PYTHON}} -B \"${CLAUDE_SKILL_DIR}/scripts/audit_ai_config.py\" *)", "Bash(git rev-parse --show-toplevel)", "PowerShell(git rev-parse --show-toplevel)", "Read", "Glob", "AskUserQuestion"]
---

# Audit AI Agent Configuration

Assess a repository's AI agent configuration across Claude Code, Codex, and Copilot and report findings by severity. The audit engine performs every deterministic check; you run it, relay its report, and add only the judgment it cannot make. Never write, modify, or delete files, and do not read the engine to redo its checks by hand. Remediation is a manual fix.

## 1. Run the engine

Confirm the target is a Git repository (`git rev-parse --show-toplevel`); if it is not, ask the user with `AskUserQuestion` which repository to audit. Then run, from anywhere:

```bash
{{PYTHON}} -B "${CLAUDE_SKILL_DIR}/scripts/audit_ai_config.py" --root "<repository root>"
```

Add `--json` when the user wants machine-readable output, and `--provenance` when they ask why a finding holds or what guidance a check follows: the report then names, for each check its findings name, the published page and the passage the check rests on, or this source's decision where no page governs it. Cite that source beside the finding.

The report states its result on one line, before the `SUMMARY` lines; report by that line, not the exit code:

| Line | Meaning | Report as |
|---|---|---|
| `RESULT COMPLIANT` | No `ERROR` findings and the authority is conforming | Compliant within statically verifiable scope |
| `RESULT ERRORS` | One or more `ERROR` findings | Errors found |
| `RESULT INCONCLUSIVE` | Authority is ambiguous, unconfigured, or an alternative source | Inconclusive; the conforming-only checks did not run |
| `FAILED <reason>` | There is no report: the root is not a Git repository, or `--provenance` could not read the provenance file | The failure |

With `--json` there is no `RESULT` line: a non-conforming `authority` is inconclusive, otherwise `exitCode` `1` means errors found.

The Markdown report writes the punctuation of each path and message as character entities, such as `&#46;` for `.`, so the repository's text cannot break its table; relay them as the characters they stand for. Relay the report's authority and scope status, every `ERROR` and `WARNING` finding, and every finding whose check is `limitation`, which names a documented limitation that applies to this repository. For the remaining `INFO` findings, relay only the engine's `SUMMARY` lines, which count findings per severity and `INFO` findings per check; give the full `INFO` list only when the user asks. `${CLAUDE_SKILL_DIR}/references/audit-policy.md` explains what every check covers, `${CLAUDE_SKILL_DIR}/references/report-schema.md` the finding format, `${CLAUDE_SKILL_DIR}/references/generated-layout.md` the generated files a conforming repository holds, and `${CLAUDE_SKILL_DIR}/references/known-limitations.md` the limitations in full; read them only to answer a question the report raises.

## 2. Review instruction content (judgment)

Only for a conforming repository, `Read` the instruction files the engine lists as effective sources for each targeted surface (the `layering` findings) and every file `Glob` matches for `.github/instructions/**/*.instructions.md`, and report as `WARNING` findings of your own:

- instructions that contradict each other within one surface's effective set, such as two sources prescribing different build commands or formatting rules;
- substantial overlap that duplicates `CLAUDE.md` guidance in a non-generated file, which will drift;
- a missing dogfooding requirement, when the repository ships tools or rules that it should also apply to itself.

Label these as your assessment, separate from the engine's findings, and do not change the engine's result because of them.

## 3. Optional operational validation

Only when the user explicitly authorizes it for a trusted repository, because both commands execute repository-controlled code:

1. Run the generator the engine found, in read-only mode, never `--write`: `{{PYTHON}} -B "<path>" --check` from the repository root, where `<path>` is the `path` of the first finding whose message begins `Generator script found:` in the engine's `--json` report, which carries it as written (the Markdown report writes punctuation as character entities). Without that finding there is no generator to run; say so.
2. Handshake each stdio MCP server, which starts it, sends `initialize` and `tools/list`, and stops it without invoking a tool:

   ```bash
   {{PYTHON}} -B "${CLAUDE_SKILL_DIR}/scripts/mcp_handshake.py" --root "<repository root>"
   ```

   The skill does not pre-approve this command, because it starts commands the repository configures, so the user approves it when it runs.

   It prints `HANDSHAKE_OK`, `HANDSHAKE_FAILED <reason>`, or `SKIPPED` (remote transports, or a server without a command or refused) per server, or `NO_SERVERS`. `CONFIG_ERROR <file> <reason>` means that file could not be read, so none of its servers was checked, or one server in it is malformed or would start outside the repository while the others ran: report it as a failure, never as "no servers". `CONFIG_WARNING <file> <reason>` names an entry that is not a server, or a `.mcp.json` or `.github/mcp.json` without its `mcpServers` key: report it as a warning. Pass `--server <name>` to start only one; `FAILED no MCP server named <name>` means none has that name.

Report the results as additional findings.
