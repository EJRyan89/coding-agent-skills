# Report Schema

Finding format, severities, and output modes for the audit engine.

## Contents

- Report Envelope
- Finding Structure
- Severities
- Exit Codes
- Ordering
- Output Formats: Markdown (default), JSON (for CI integration)
- Manual-Verification Warnings
- No Remediation

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
| `path` | string | File path relative to repo root (when applicable; JSON `null` and an empty Markdown cell otherwise) |
| `line` | int | Line number (when applicable, 1-indexed; JSON `null` and an empty Markdown cell otherwise) |
| `message` | string | Human-readable description of the finding |

## Severities

| Severity | Meaning | Exit code impact |
|---|---|---|
| `ERROR` | Actionable, blocks compliance. Missing generated files, drift, ownership failures. | Causes `RESULT ERRORS` and exit code 1 |
| `WARNING` | Should address. Manual-verification items, potential issues. | Does not change exit code |
| `INFO` | Informational. Inventory observations, authority classification result. | Does not change exit code |

## Exit Codes

The Markdown report names its result on one `RESULT` line, before the `SUMMARY` lines. Key on that line, not on
the exit code, which `ERRORS` and `INCONCLUSIVE` share:

| Result line | Code | Meaning |
|---|---|---|
| `RESULT COMPLIANT` | `0` | Compliant within statically verifiable scope |
| `RESULT ERRORS` | `1` | One or more `ERROR`-level findings |
| `RESULT INCONCLUSIVE` | `1` | Ambiguous, unconfigured, or alternative authority; cannot confirm compliance or non-compliance |
| `FAILED <reason>` | `1` | No report: the root is not a Git repository |
| (usage text on stderr) | `2` | A usage error: arguments the engine rejects |

Ambiguous repositories must not return `0`. `INCONCLUSIVE` takes precedence over `ERROR` findings. JSON output has no
`RESULT` line: a non-conforming `authority` is inconclusive, otherwise `exitCode` `1` means `ERROR` findings.

## Ordering

Deterministic: sorted by severity (ERROR first), then by file path, then by line number.

## Output Formats

### Markdown (default)

```markdown
## AI Config Audit — {repo_name}

Authority: **conforming**
Scope: **independently-derived**

RESULT ERRORS
SUMMARY ERROR 1
SUMMARY WARNING 1
SUMMARY INFO 1
SUMMARY INFO inventory 1

### Findings

| Severity | Check | Path | Line | Message |
|---|---|---|---|---|
| ERROR | copilot-config | .github/skills/demo/SKILL.md | 4 | Frontmatter must use single-line key: value entries or block scalars |
| WARNING | mcp |  |  | Copilot repository MCP (cloud agent/code review) configured via repository settings — cannot validate statically |
| INFO | inventory | CLAUDE.md |  | Found CLAUDE.md |
```

`Authority` and `Scope` print the same lowercase values as the JSON `authority` and `scopeStatus`. A finding with no
path or line leaves that cell empty.

The `SUMMARY` lines come before the findings: one per severity, always in `ERROR`, `WARNING`, `INFO` order and
printed even when the count is `0`, then one per check that produced an `INFO` finding, sorted by check name. JSON
output has no summary; count its `findings` instead.

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
      "check": "copilot-config",
      "path": ".github/skills/demo/SKILL.md",
      "line": 4,
      "message": "Frontmatter must use single-line key: value entries or block scalars"
    },
    {
      "severity": "WARNING",
      "check": "mcp",
      "path": null,
      "line": null,
      "message": "Copilot repository MCP (cloud agent/code review) configured via repository settings — cannot validate statically"
    },
    {
      "severity": "INFO",
      "check": "inventory",
      "path": "CLAUDE.md",
      "line": null,
      "message": "Found CLAUDE.md"
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

The audit never fixes findings. Remediation is a manual fix; `generated-layout.md` describes the generated files the checks expect.
