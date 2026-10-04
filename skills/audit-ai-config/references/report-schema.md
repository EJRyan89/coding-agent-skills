# Report Schema

Finding format, severities, and output modes for the audit engine.

## Report Envelope

The Markdown and JSON report include repository `authority` plus `scopeStatus`.
`scopeStatus` is `independently-derived` when literal generator target declarations
were statically compared to the manifest, `manifest-declared-only` when that safe
derivation was unavailable, `no-declared-scope` when there is neither a manifest nor a
recognized generator, or `not-applicable` before authority is conforming.

## Finding Structure

Each finding contains:

| Field | Type | Description |
|---|---|---|
| `severity` | enum | `ERROR`, `WARNING`, or `INFO` |
| `check` | string | Which audit check produced this finding (e.g., `parity`, `orphan`, `mcp`, or `limitation` for an applicable known limitation) |
| `path` | string | File path relative to repo root (when applicable) |
| `line` | int | Line number (when applicable, 1-indexed) |
| `message` | string | Human-readable description of the finding |
| `detail` | string | Additional context (diff, expected vs actual, etc.) |

## Severities

| Severity | Meaning | Exit code impact |
|---|---|---|
| `ERROR` | Actionable, blocks compliance. Missing generated files, drift, ownership failures. | Causes exit code 1 |
| `WARNING` | Should address. Manual-verification items, potential issues. | Does not change exit code |
| `INFO` | Informational. Inventory observations, authority classification result. | Does not change exit code |

## Exit Codes

| Code | Meaning |
|---|---|
| `0` | Compliant within statically verifiable scope |
| `1` | One or more `ERROR`-level findings |
| `2` | `INCONCLUSIVE` — ambiguous, unconfigured, or alternative authority; cannot confirm compliance or non-compliance |

Ambiguous repositories must not return `0`.

## Ordering

Deterministic: sorted by severity (ERROR first), then by file path, then by line number.

## Output Formats

### Markdown (default)

```markdown
## AI Config Audit — {repo_name}

Authority: **Conforming** (manifest found)
Scope: **independently-derived**

### Findings

| Severity | Check | Path | Message |
|---|---|---|---|
| ERROR | parity | .github/copilot-instructions.md | Content drift from CLAUDE.md |
| WARNING | mcp | (repository settings) | Copilot repository MCP cannot be validated statically |
| INFO | inventory | CLAUDE.md | Found at repository root |
```

### JSON (for CI integration)

```json
{
  "repository": "{repo_name}",
  "authority": "conforming",
  "scopeStatus": "independently-derived",
  "exitCode": 1,
  "findings": [
    {
      "severity": "ERROR",
      "check": "parity",
      "path": ".github/copilot-instructions.md",
      "line": null,
      "message": "Content drift from CLAUDE.md",
      "detail": "--- .github/copilot-instructions.md\n+++ expected/..."
    }
  ]
}
```

## Manual-Verification Warnings

These are emitted per surface when static validation is insufficient:

| Surface condition | Warning |
|---|---|
| `copilot_repository` targeted | Repository MCP settings cannot be validated statically |
| Code review targeted | Custom-instructions enablement is unverifiable |
| VS Code targeted | `chat.useClaudeMdFile`, `chat.useAgentsMdFile`, `useInstructionFiles`, `includeApplyingInstructions` settings are unverifiable |
| `copilot_local` targeted | Folder trust status cannot be determined statically |
| Copilot CLI/app, cloud agent, or code review targeted | Repository settings, organization policy, authentication, model availability, runtime enablement, and actual use cannot be verified statically |
| Code review targeted | Instructions, agents, and skills come from the PR head; this is not a trusted-base or trusted-ref review contract |
| Generator scope is not safely derivable | Editable manifest scope can suppress checks; report is `manifest-declared-only` |

## No Remediation

The audit never fixes findings. Remediation is only performed by separately invoking `init-ai-config` or manual fixes.
