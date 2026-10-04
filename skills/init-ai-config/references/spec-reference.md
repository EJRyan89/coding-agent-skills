# Spec Reference

The JSON spec is the only repository-specific input to the generator. `init_ai_config.py install` validates it and writes each value as a literal constant in `.github/scripts/ai_config.py`, so the audit can read the declared scope statically. `references/example-spec.json` is a complete example.

## Fields

| Key | Generator constant | Type | Default | Meaning |
|---|---|---|---|---|
| `runtimes` | `TARGET_RUNTIMES` | list of strings, required | | `claude` and optionally `codex` |
| `surfaces` | `TARGET_SURFACES` | list of strings, required | | Any of `copilot_cli`, `copilot_app`, `vscode`, `jetbrains`, `cloud_agent`, `code_review`; may be empty |
| `features` | `TARGET_FEATURES` | list of strings | `[]` | `ci_parity`, and `ci_parity_caller`, which requires `ci_parity` |
| `copilot_title` | `COPILOT_TITLE` | string | `# {repo_name} — AI Coding Instructions` | First line of the Copilot projection; `{repo_name}` is the only placeholder |
| `copilot_sections` | `COPILOT_SECTIONS` | list of strings | The four required sections | `CLAUDE.md` H2 headings projected into `.github/copilot-instructions.md`; each must exist |
| `copilot_required_sections` | `COPILOT_REQUIRED_SECTIONS` | list of strings | Overview, Build and Test Commands, Formatting Rules, CI / Quality Gates | Must be a subset of `copilot_sections` |
| `copilot_setup_commands` | `COPILOT_SETUP_COMMANDS` | list of `{"name", "run"}` objects | `[]` | One named step each in the cloud-agent setup workflow |
| `mcp_servers` | `MCP_SERVERS` | list of objects | `[]` | Server definitions; see below |

Unknown keys are rejected. Keep the Copilot projection concise: it serves the least-capable targeted surface, and the required sections are enforced when JetBrains, VS Code, cloud agent, or code review is targeted. Architecture, step-by-step workflows, and MCP tool details belong in `CLAUDE.md` and skills, not the projection.

## What each choice generates

| Choice | Generated files |
|---|---|
| Any Copilot surface | `.github/copilot-instructions.md` |
| `codex` runtime | `AGENTS.md` adapter, `.agents/skills/<name>/SKILL.md` shim per `.claude/skills` skill, and `.codex/config.toml` when a server targets `codex` |
| Server targeting `claude` | `.mcp.json` (also read by Copilot CLI/app) |
| Server targeting `copilot_local` but not `claude` | `.github/mcp.json` |
| Server targeting `vscode`, with the `vscode` surface | `.vscode/mcp.json` |
| `ci_parity` feature | `.github/workflows/ai-config-parity.yml`, a reusable `workflow_call` workflow that runs the generator tests and `--check` |
| `ci_parity_caller` feature | `.github/workflows/ai-config-parity-pr.yml`, a pull-request workflow that calls the parity workflow; choose it unless the repository's own CI calls the parity workflow, since `--check` reports `NO CALLER` otherwise |
| `cloud_agent` surface | `.github/workflows/copilot-setup-steps.yml` |
| Always | `.github/ai-config-manifest.json` |

## MCP server objects

| Field | Meaning |
|---|---|
| `name` | Unique server name |
| `targets` | Any of `claude`, `codex`, `copilot_local`, `vscode`, `copilot_repository` |
| `transport` | `stdio`, `local`, `http`, or `sse`; must support every target (matrix below) |
| `command`, `args`, `cwd` | Required `command` for `stdio` and `local` |
| `url` | Required for `http` and `sse` |
| `env` | String-to-string map of literal values; `.codex/config.toml` receives it for stdio servers only |
| `copilot_local.tools` | Must be `null` when the server also targets `claude`, because the shared `.mcp.json` cannot enforce an allowlist; a list is emitted into `.github/mcp.json` |
| `oauth` | Rejected with the `copilot_repository` target |

Other fields, such as Codex approval policy or repository tool lists, are kept in the spec but not emitted (see `known-limitations.md`).

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

## MCP tool risk classification

Classify every tool a server exposes and record it in the `MCP Tools` table of `CLAUDE.md`:

- **Read-only** (search, list, analyze): approve. When allowlisted for repository MCP and annotated `readOnlyHint: true`, code review may use it.
- **Mutating** (build, format, fix): `writes` approval when its annotations are trustworthy, otherwise prompt. Never `readOnlyHint: true`.
- **Destructive** (delete, reset): always prompt. Never `readOnlyHint: true`.
