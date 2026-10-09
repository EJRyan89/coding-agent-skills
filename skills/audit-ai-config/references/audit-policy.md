# Audit Policy

What the audit engine checks and why. Each check maps to a function in `scripts/audit_ai_config.py`; the `check` column of a finding names it. Agents run the engine rather than repeating these checks by hand. `generated-layout.md` describes the generated files and manifest that the checks after authority classification expect.

## Contents

- Read-Only Safety Model
- Order of Checks
- Checks: 1. Inventory, 2. Authority Classification, 3. Vocabulary and Scope Integrity, 4. Parity, 5. Orphans,
  6. MCP Configuration, 7. Instruction Layering, 8. Copilot Skills, Agents, and Roles, 9. Behavioral Constraints,
  10. Ownership, 11. User-Authored Collisions, 12. Applicable Limitations

## Read-Only Safety Model

The engine never writes, modifies, or deletes files. It does not run generators, tests, hooks, MCP servers, package installers, authentication commands, or autofix modes, and it makes no network requests. All parsing is static. An MCP server can mutate external state even when the audit writes no files, so the handshake in `scripts/mcp_handshake.py` and the generator's `--check` are a separate opt-in step that needs explicit user authorization.

## Order of Checks

1. Inventory runs for every repository.
2. Authority classification decides whether the remaining checks run. Only a **conforming** repository receives checks 3 to 12; any other classification stops the audit with `RESULT INCONCLUSIVE` (exit code `1`).

## Checks

### 1. Inventory (`inventory`)

Reports each AI configuration file found as an `INFO` finding: `CLAUDE.md`, `GEMINI.md`, `REVIEW.md`, `.codex/config.toml`, `.mcp.json`, `.github/mcp.json`, `.vscode/mcp.json`, `.github/copilot-instructions.md`, `.github/workflows/copilot-setup-steps.yml`, `.github/ai-config-manifest.json`, every `AGENTS.md` and `AGENTS.override.md` at any depth outside version-control and dependency directories (`.git`, `.hg`, `.svn`, `node_modules`, `.venv`, `venv`, `__pycache__`, `.tox`, `.nox`), which no nested-file check walks into, skills under `.claude/skills`, `.agents/skills`, and `.github/skills`, custom agents under `.github/agents` and `.claude/agents`, path-specific `.github/instructions/**/*.instructions.md`, a generator at `.github/scripts/ai_config.py` or `scripts/ai_config.py`, and any workflow mentioning `ai-config` or `ai_config`.

**Why**: Establishes the baseline and surfaces files that may conflict, overlap, or be orphaned.

### 2. Authority Classification (`authority`)

Independent signals:

1. A valid manifest at `.github/ai-config-manifest.json` with `generatedBy: ai_config.py`, `canonicalSource: CLAUDE.md`, a valid schema, an existing canonical source, and at least one derived artifact.
2. `CLAUDE.md` exists at the repository root.
3. `CLAUDE.md` has a `## Maintaining AI Agent Config` section.
4. A generator script references `CLAUDE.md`.
5. A workflow references `ai_config` together with `--check`.

| Classification | Criteria | Result |
|---|---|---|
| **Conforming** | Signal 1 alone, or two or more of signals 2 to 5 | `COMPLIANT` or `ERRORS` |
| **Alternative** | The manifest names a canonical source other than `CLAUDE.md` | `INCONCLUSIVE` |
| **Ambiguous** | Exactly one of signals 2 to 5 | `INCONCLUSIVE` |
| **Unconfigured** | No signals | `INCONCLUSIVE` |

A malformed or non-object manifest is a `WARNING` and is not a signal; a structurally valid manifest that fails schema validation produces `ERROR` findings.

**Why**: Prevents false drift reports on repositories that do not use `CLAUDE.md` as canonical. Ordinary user-authored `AGENTS.md` or Copilot instructions are not drift unless the repository is conforming.

### 3. Vocabulary and Scope Integrity (`vocabulary`, `scope`)

Manifest runtimes, surfaces, and MCP targets must use the canonical names. The manifest is editable, so it cannot be the only trust source for its own scope: when a recognized generator exists, its literal `TARGET_RUNTIMES`, `TARGET_SURFACES`, and `TARGET_FEATURES` assignments are read with Python AST only, and a manifest disagreement is an `ERROR` (`scopeStatus: independently-derived`). When the values cannot be derived safely, the report is `manifest-declared-only` with a `WARNING`. With neither a manifest nor a generator, it is `no-declared-scope` with an `INFO` finding that target-specific checks were not run.

### 4. Parity (`parity`)

For each manifest artifact: the path must be in the fixed allowlist, without traversal, backslashes, drive letters, or UNC prefixes; the file must exist; a JSON artifact's content must match its manifest hash (a mismatch is a user-modification conflict, not drift); a comment-supporting artifact must carry the `AUTO-GENERATED from CLAUDE.md` marker. `AGENTS.md`, `.agents/skills/*/SKILL.md` shims, and `.github/copilot-instructions.md` (the sections named by the manifest's `copilot_sections`) are compared with their deterministic expected content. All failures are `ERROR`.

**Why**: Stale derived files make runtime behavior diverge from the authority.

### 5. Orphans (`orphan`)

`WARNING` for an `.agents/skills/*/SKILL.md` shim whose canonical `.claude/skills/*/SKILL.md` no longer exists, and, when a manifest exists, for a marker-bearing file at a generator-owned path that the manifest no longer lists.

**Why**: Orphaned files carry outdated instructions and confuse ownership.

### 6. MCP Configuration (`mcp`)

Parsed statically, never started:

- `.mcp.json` and `.github/mcp.json` need an `mcpServers` object; each server needs `command` or `url`.
- A server name in both `.mcp.json` and `.github/mcp.json` is an `ERROR`: `.mcp.json` wins, so the other entry is unreachable.
- `.vscode/mcp.json` needs a `servers` wrapper, not `mcpServers`.
- `.codex/config.toml` must parse; an HTTP server there must not carry `env` or `env_vars`.
- Each manifest server's transport must support each of its targets (the transport matrix below).
- A `tools` allowlist other than `["*"]` on a server in the shared `.mcp.json` is an `ERROR` when `copilot_local` is targeted, because the shared file cannot enforce it.
- The same server name in several files with different `command`, `args`, `url`, `cwd`, or `env` is a `WARNING`.
- Targeting `copilot_repository` always produces a manual-verification `WARNING`, plus a `readOnlyHint` warning when code review is targeted.

| Transport | `claude` | `codex` | `copilot_local` | `vscode` | `copilot_repository` |
|---|---|---|---|---|---|
| `stdio` | Yes | Yes | Yes | Yes | Yes |
| `local` | No | No | Yes | No | Yes |
| `http` | Yes | Yes | Yes | Yes | Yes |
| `sse` | Yes | No | Yes | Yes | Yes |

**Why**: MCP misconfiguration is silent; a runtime just does not see the server.

### 7. Instruction Layering (`layering`, `trust-boundary`, `runtime`)

Each targeted surface is resolved with its own algorithm:

| Surface | Sources it reads | Unsatisfied (`ERROR`) when |
|---|---|---|
| Codex | Per directory from root to cwd: `AGENTS.override.md`, else `AGENTS.md`, else fallbacks such as `CLAUDE.md` | Neither `AGENTS.md` nor `CLAUDE.md` exists, or a root `AGENTS.override.md` masks the generated adapter |
| Copilot CLI/app | `CLAUDE.md`, `AGENTS.md`, `.github/copilot-instructions.md`, `GEMINI.md`, path-specific instructions, combined | No source exists |
| Cloud agent | Nearest `AGENTS.md`; without one, root `CLAUDE.md`, then `GEMINI.md` | None of them exists |
| Code review | A non-redirecting `AGENTS.md`, `.github/copilot-instructions.md`, path-specific instructions; never `CLAUDE.md` or `GEMINI.md` | None of them exists, including when `AGENTS.md` only redirects to `CLAUDE.md` |
| JetBrains | `.github/copilot-instructions.md` and path-specific instructions only | Neither exists |
| VS Code | All sources, each gated by a setting | Never; a `WARNING` says the settings are unverifiable |

Nested `AGENTS.md` and `AGENTS.override.md` files under a redirecting root adapter produce `WARNING` findings for review. Code review also produces a `trust-boundary` `WARNING`: GitHub loads its instructions, agents, and skills from the PR head, which is advisory context, not a trusted-base review contract. Copilot surfaces produce a `runtime` `WARNING` that settings, policy, authentication, model availability, and enablement are not statically verifiable.

The engine does not judge whether the effective sources contradict or overlap each other; the skill asks the agent to review that.

### 8. Copilot Skills, Agents, and Roles (`copilot-skill`, `copilot-agent`, `collision`, `provenance`, `runtime-role`)

Skills in `.github/skills`, `.claude/skills`, and `.agents/skills` need `SKILL.md` with frontmatter whose entries are single-line `key: value` pairs or block scalars (`|` or `>`, read as YAML reads them, since Copilot and the Agent Skills specification define the frontmatter as YAML), a lowercase hyphenated `name` matching the directory, a description, and no empty `allowed-tools`. Custom agents in `.github/agents` and `.claude/agents` need a `.md` or `.agent.md` filename, a description, `target` of `vscode` or `github-copilot`, boolean switches, non-empty tools, and `modelPolicy` of `preferred` or `required`. A skill or agent named `code-review` collides with Copilot's built-in agent (`ERROR`). A marker-bearing projection in `.github/skills` or `.github/agents` must be manifest-owned and vice versa. A file that cannot be read or decoded is an `ERROR`, never a crash.

Optional manifest `runtimeRoles` must map `copilot_cli` and `copilot_app` to `full_local_host`, `cloud_agent` to `deferred_remote_worker`, and `code_review` to `advisory_evidence_only`. Missing declarations are a `WARNING`; wrong ones are an `ERROR`. The audit stops at wiring and declared boundaries: it does not run a Copilot session, invoke MCP, or validate review results.

### 9. Behavioral Constraints (`behavioral`)

`WARNING` when `CLAUDE.md` lacks a "never disable" or "never suppress" rule, a rule forbidding workarounds for failing checks, or a quality gate (a coverage threshold, a lint severity level, or a rule that every check must pass). Gate detection is a text heuristic.

**Why**: These guardrails stop agents from weakening the repository's own quality standards.

### 10. Ownership (`ownership`)

Every manifest artifact needs ownership tracking: an embedded marker for comment-supporting files and a manifest hash for JSON files. Missing tracking is an `ERROR`.

**Why**: Without ownership, the generator cannot safely update or delete files, and a forged manifest must not authorize deleting arbitrary files.

### 11. User-Authored Collisions (`collision`)

An unmarked `AGENTS.md` when Codex is targeted, or an unmarked `.github/copilot-instructions.md` when a Copilot surface is targeted, is an `ERROR`: the generator refuses to overwrite it, so it must be resolved deliberately.

### 12. Applicable Limitations (`limitation`)

`INFO` findings for each documented limitation this repository hits: marker-only parity for TOML and YAML artifacts, a manifest without `copilot_sections`, provenance-only Copilot projections, unanalyzed path-specific instructions, and nested `.mcp.json` files. They never change the exit code. See `known-limitations.md` for detail.
