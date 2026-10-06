# Generated Layout

The layout a repository's own generator, `.github/scripts/ai_config.py`, produces from `CLAUDE.md`. The audit checks it in a conforming repository, as described in `audit-policy.md`. This source no longer ships a skill that writes this layout. The page records what the audit expects, so that a finding about a generated file can be explained and the file fixed by hand.

## Declared scope

The generator declares its scope as literal assignments, which the scope check reads statically, and records the same values in `.github/ai-config-manifest.json`:

| Generator constant | Manifest key | Values |
|---|---|---|
| `TARGET_RUNTIMES` | `runtimes` | `claude`, optionally `codex` |
| `TARGET_SURFACES` | `surfaces` | Any of `copilot_cli`, `copilot_app`, `vscode`, `jetbrains`, `cloud_agent`, `code_review`; may be empty |
| `TARGET_FEATURES` | `features` | `ci_parity`, and `ci_parity_caller`, which requires `ci_parity` |
| `COPILOT_SECTIONS` | `copilot_sections` | `CLAUDE.md` H2 headings projected into `.github/copilot-instructions.md` |

When `copilot_sections` is absent, the projection holds the four default sections: Overview, Build and Test Commands, Formatting Rules, and CI / Quality Gates.

## What each choice generates

| Choice | Generated files |
|---|---|
| Any Copilot surface | `.github/copilot-instructions.md` |
| `codex` runtime | `AGENTS.md` adapter, `.agents/skills/<name>/SKILL.md` shim per `.claude/skills` skill, and `.codex/config.toml` when a server targets `codex` |
| Server targeting `claude` | `.mcp.json` (also read by Copilot CLI/app) |
| Server targeting `copilot_local` but not `claude` | `.github/mcp.json` |
| Server targeting `vscode`, with the `vscode` surface | `.vscode/mcp.json` |
| `ci_parity` feature | `.github/workflows/ai-config-parity.yml`, a reusable `workflow_call` workflow that runs the generator tests and `--check` |
| `ci_parity_caller` feature | `.github/workflows/ai-config-parity-pr.yml`, a pull-request workflow that calls the parity workflow |
| `cloud_agent` surface | `.github/workflows/copilot-setup-steps.yml` |
| Always | `.github/ai-config-manifest.json` |

## MCP server objects

The manifest's `mcp_servers` entries:

| Field | Meaning |
|---|---|
| `name` | Unique server name |
| `targets` | Any of `claude`, `codex`, `copilot_local`, `vscode`, `copilot_repository` |
| `transport` | `stdio`, `local`, `http`, or `sse`; must support every target (matrix below) |
| `command`, `args`, `cwd` | Required `command` for `stdio` and `local` |
| `url` | Required for `http` and `sse` |
| `env` | String-to-string map of literal values; `.codex/config.toml` receives it for stdio servers only |
| `copilot_local.tools` | Must be `null` when the server also targets `claude`, because the shared `.mcp.json` cannot enforce an allowlist |

### Transport compatibility

| Transport | Claude Code | Codex | Copilot CLI/app | VS Code | Copilot repository |
|---|---|---|---|---|---|
| `stdio` | Yes | Yes | Yes | Yes | Yes |
| `local` | No | No | Yes | No | Yes |
| `http` | Yes | Yes | Yes | Yes | Yes |
| `sse` | Yes | No | Yes | Yes | Yes |

### Copilot surface capabilities

| Surface | `CLAUDE.md` | `AGENTS.md` | `.github/copilot-instructions.md` | Path-specific | Skills | File MCP | Repository MCP |
|---|---|---|---|---|---|---|---|
| VS Code | Conditional | Conditional | Conditional | Conditional | Yes | `.vscode/mcp.json` | No |
| JetBrains | No | No | Yes | Yes | Yes | Manual | No |
| Copilot app | Yes | Yes | Yes | Yes | Yes | `.mcp.json`, `.github/mcp.json` | No |
| CLI | Yes | Yes | Yes | Yes | Yes | `.mcp.json`, `.github/mcp.json` | No |
| Cloud agent | Indirect | Nearest wins | Yes | Yes | Yes | No | Yes |
| Code review | No | When enabled | When enabled | When enabled | Yes | No | Yes |

Copilot CLI checks repository skills in `.github/skills`, then `.agents/skills`, then `.claude/skills`, and personal `~/.copilot/skills` before `~/.agents/skills`, so a higher-priority skill can shadow a generated shim.
