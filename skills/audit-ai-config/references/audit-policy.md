# Audit Policy

What the audit engine checks and why. Each check maps to a function in `scripts/audit_ai_config.py`; the `check` column of a finding names it. `provenance.json` names, for each check, the published page and the passage it rests on, or this source's decision where no page governs it. Agents run the engine rather than repeating these checks by hand. `generated-layout.md` describes the generated files and manifest that the checks after authority classification expect.

## Contents

- Read-Only Safety Model
- Order of Checks
- Checks: 1. Inventory, 2. Authority Classification, 3. Vocabulary and Scope Integrity, 4. Parity, 5. Orphans,
  6. MCP Configuration, 7. Instruction Layering, 8. Skills, Agents, and Roles, 9. Behavioral Constraints,
  10. Ownership, 11. User-Authored Collisions, 12. Claude Code Project Configuration, 13. Codex Project
  Configuration, 14. Applicable Limitations

## Read-Only Safety Model

The engine never writes, modifies, or deletes files. It does not run generators, tests, hooks, MCP servers, package installers, authentication commands, or autofix modes, and it makes no network requests. All parsing is static. An MCP server can mutate external state even when the audit writes no files, so the handshake in `scripts/mcp_handshake.py` and the generator's `--check` are a separate opt-in step that needs explicit user authorization.

## Order of Checks

1. Inventory runs for every repository.
2. Authority classification decides whether the remaining checks run. Only a **conforming** repository receives checks 3 to 14; any other classification stops the audit with `RESULT INCONCLUSIVE` (exit code `1`).

## Checks

### 1. Inventory (`inventory`)

Reports each AI configuration file found as an `INFO` finding: `CLAUDE.md`, `.claude/CLAUDE.md`, `CLAUDE.local.md`, `.claude/settings.json`, `GEMINI.md`, `REVIEW.md`, `.codex/config.toml`, `.codex/hooks.json`, Codex's custom agents (`.codex/agents/*.toml`) and rules (`.codex/rules/*.rules`), `.mcp.json`, `.github/mcp.json`, `.vscode/mcp.json`, `.github/copilot-instructions.md`, `.github/workflows/copilot-setup-steps.yml`, `.github/ai-config-manifest.json`, every `AGENTS.md` and `AGENTS.override.md` at any depth outside version-control, dependency, build-output, and Visual Studio folders (`.git`, `.hg`, `.svn`, `node_modules`, `.venv`, `venv`, `__pycache__`, `.tox`, `.nox`, `bin`, `obj`, `.vs`) and nested checkouts (a folder holding a `.git` folder or file: a clone, submodule, or worktree), which no nested-file check walks into, skills under `.claude/skills`, `.agents/skills`, and `.github/skills`, custom agents under `.github/agents` and `.claude/agents`, Claude Code's rules (every `.md` file below `.claude/rules`), path-specific `.github/instructions/**/*.instructions.md`, a generator at `.github/scripts/ai_config.py` or `scripts/ai_config.py`, and any workflow mentioning `ai-config` or `ai_config`.

A file a check reads that cannot be read or decoded is an inventory `ERROR`, `Could not read, so the checks that read it did not run`, so an audit that skipped those checks is never `COMPLIANT`: `CLAUDE.md`, `.claude/CLAUDE.md`, `CLAUDE.local.md`, `GEMINI.md`, `REVIEW.md`, the manifest, `.github/copilot-instructions.md`, each generator, every `AGENTS.md`, and every workflow. The other files a check reads report their own read failure, as the checks below say.

No read in any check follows a symbolic link or junction that leads out of the repository root. A walk or directory listing skips the link, so nothing behind it is read or named. A file at a fixed path, such as `CLAUDE.md`, `.claude/settings.json`, `.codex/config.toml`, or a skill's `agents/openai.yaml`, that is such a link or lies behind one is never read either: each check that reads it reports it as a file it could not read, giving the reason `it lies behind <link>, a link that leads out of the repository, which the audit does not follow`, so a canonical source behind one is the inventory `ERROR` above, and the inventory names the link in place of the file. A check that asks only whether a fixed file exists, as the authority, scope, and layering checks do, still counts it, since the runtime that reads it follows the link. Either way the link itself is reported once as an inventory `WARNING` at its path, `Link leads out of the repository; the audit does not follow it`. A link whose target stays inside the root is followed, but never back into a folder the walk is already inside, so a link to the root or to one of its own parents is walked once.

**Why**: Establishes the baseline and surfaces files that may conflict, overlap, or be orphaned.

### 2. Authority Classification (`authority`)

Independent signals:

1. A valid manifest at `.github/ai-config-manifest.json` with `generatedBy: ai_config.py`, `canonicalSource: CLAUDE.md`, a valid schema, an existing canonical source, and at least one derived artifact.
2. `CLAUDE.md` exists at the repository root, or at `.claude/CLAUDE.md`, which Claude Code's memory page reads as the project's `CLAUDE.md`.
3. Either `CLAUDE.md` has a `## Maintaining AI Agent Config` section.
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

Manifest runtimes, surfaces, and MCP targets must use the canonical names. The manifest is editable, so it cannot be the only trust source for its own scope: when a recognized generator exists, its literal `TARGET_RUNTIMES`, `TARGET_SURFACES`, and `TARGET_FEATURES` assignments are read with Python AST only, and a manifest disagreement is an `ERROR` (`scopeStatus: independently-derived`). A derived scope is compared only with a valid manifest: a missing one, or one the authority check does not accept as an `ai_config.py` manifest of `CLAUDE.md` (malformed, failing its schema, or declaring no derived artifacts), is one `ERROR` saying the scope was not compared. When the values cannot be derived safely, or no recognized generator exists, the report is `manifest-declared-only` with a `WARNING` whose message names the reason (`report-schema.md` lists them). With neither a manifest nor a generator, the repository's own files show the scope (`file-derived`), named by a `scope` `INFO` finding, and the instruction-layering checks (check 7) run over it: the `claude` runtime when a `CLAUDE.md` or `.claude/CLAUDE.md` exists, or a root `AGENTS.md`, which Claude Code's memory page has it read in place of a `CLAUDE.md`, from the version a `scope` `INFO` at `AGENTS.md` names and while no `CLAUDE.local.md` is present; the `codex` runtime when a root `AGENTS.md` or `AGENTS.override.md`, a `.codex` folder, or `.agents/skills` exists; the `copilot_cli` and `code_review` surfaces when a `.github` folder exists, since Copilot's CLI and app and code review read a GitHub repository's `CLAUDE.md` and `AGENTS.md` with no setting of its own; and `cloud_agent` when `.github/workflows/copilot-setup-steps.yml` exists too. JetBrains and VS Code read instructions only through settings or Copilot's own files, which no file shows, so only a manifest targets them. A file-derived scope declares no MCP target, so the manifest-driven MCP checks (check 6) and runtime roles stay unrun, and the collision check (check 11) keeps to a declared scope, because it guards a generator's writes and a repository without one has nothing that writes. When no file shows a runtime or surface either, the status is `no-declared-scope` with an `INFO` finding that target-specific checks were not run.

### 4. Parity (`parity`)

For each manifest artifact: the path must be in the fixed allowlist, without traversal, backslashes, drive letters, or UNC prefixes; the file must exist; an artifact's content must match its manifest hash, which every JSON artifact needs and any other artifact may carry (a mismatch is a user-modification conflict, not drift). `AGENTS.md`, `.agents/skills/*/SKILL.md` shims, and `.github/copilot-instructions.md` (the sections named by the manifest's `copilot_sections`) are compared with their deterministic expected content. All failures are `ERROR`, one per artifact: the first check it fails, in that order. The ownership marker is the `ownership` check's.

**Why**: Stale derived files make runtime behavior diverge from the authority.

### 5. Orphans (`orphan`)

`WARNING` for an `.agents/skills/*/SKILL.md` shim whose canonical `.claude/skills/*/SKILL.md` no longer exists, and, when a manifest exists, for a marker-bearing file at a generator-owned path that the manifest no longer lists.

**Why**: Orphaned files carry outdated instructions and confuse ownership.

### 6. MCP Configuration (`mcp`)

Parsed statically, never started:

- `.mcp.json` and `.github/mcp.json` need an `mcpServers` object; each server needs `command` or `url`.
- A `.mcp.json` server Claude Code skips is an `ERROR`: one with a `url` and no `type`, which it reads as stdio, one of `type` `sdk`, which only an SDK host can register, and one with a reserved built-in name (`workspace`, `claude-in-chrome`, `computer-use`, `Claude Preview`, or `Claude Browser`).
- A server name in both `.mcp.json` and `.github/mcp.json` is an `ERROR`: `.mcp.json` wins, so the other entry is unreachable.
- `.vscode/mcp.json` needs a `servers` object, and a missing one is an `ERROR` (`mcpServers` is the usual mistake); each server needs `command` or `url`. A missing `mcpServers` in the other two files, or an entry that is not an object, is a `WARNING`.
- One reader reads each JSON file for the audit and for the handshake, so both report the same problems for it.
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
| Codex | Per directory from root to cwd, the first that exists of `AGENTS.override.md`, `AGENTS.md`, and the names `project_doc_fallback_filenames` gives in `.codex/config.toml`, skipped when it holds only whitespace; `CLAUDE.md` only as such a name | No root `AGENTS.override.md`, `AGENTS.md`, or `CLAUDE.md` exists, the root file Codex chooses is empty, or a root `AGENTS.override.md` masks `CLAUDE.md` or the generated adapter |
| Copilot CLI/app | `CLAUDE.md`, `AGENTS.md`, `.github/copilot-instructions.md`, `GEMINI.md`, path-specific instructions, and the rules below `.claude/rules`, which its command reference reads as it reads path-specific instructions, combined | No source exists |
| Cloud agent | Nearest `AGENTS.md`; without one, root `CLAUDE.md`, then `GEMINI.md` | None of them exists |
| Code review | From the pull request's head branch: a non-redirecting root `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, `REVIEW.md`, `.github/copilot-instructions.md`, path-specific instructions | None of them exists; an `AGENTS.md` that only redirects to `CLAUDE.md` counts only through the `CLAUDE.md` it names |
| JetBrains | `.github/copilot-instructions.md` and path-specific instructions only | Neither exists |
| VS Code | All sources, each gated by a setting | Never; a `WARNING` says the settings are unverifiable |

Nested `AGENTS.md` and `AGENTS.override.md` files under a redirecting root adapter produce `WARNING` findings for review, except an `AGENTS.md` beside an `AGENTS.override.md`, which Codex never loads. A root `CLAUDE.md` that `.codex/config.toml` does not name as a fallback is a `WARNING`: Codex loads it only when a user's own configuration names it, which a static audit cannot read; a fallback other than `CLAUDE.md` that Codex chooses is a `WARNING` too. The root file Codex chooses is a `WARNING` when it is larger than `project_doc_max_bytes` (32 KiB unless `.codex/config.toml` sets another value), naming its size and the cap: Codex stops adding instructions at the cap, so it reads the file only in part, and a user's own Codex configuration may set another cap, which a static audit cannot read. Each instruction file code review reads, the path-specific ones among them, is a `WARNING` when it is over the 1,000 lines GitHub's customize-code-review page advises for one file, beyond which its reviews may deteriorate; a path-specific one it cannot read is an `ERROR`. Code review also produces a `trust-boundary` `WARNING`: GitHub loads its instructions, agents, and skills from the PR head, which is advisory context, not a trusted-base review contract. Copilot surfaces produce a `runtime` `WARNING` that settings, policy, authentication, model availability, and enablement are not statically verifiable.

The engine does not judge whether the effective sources contradict or overlap each other; the skill asks the agent to review that.

### 8. Skills, Agents, and Roles (`copilot-config`, `copilot-skill`, `copilot-agent`, `agent-skill`, `claude-skill`, `claude-agent`, `codex-skill`, `collision`, `provenance`, `runtime-role`)

Skills in `.github/skills`, `.claude/skills`, and `.agents/skills` need `SKILL.md` with frontmatter that YAML reads, since Copilot and the Agent Skills specification define the frontmatter as YAML: `key: value` entries whose values are plain or quoted scalars (read without their quotes, and a plain value without a trailing ` # comment`), block scalars (`|` or `>`), flow or block lists, or nested mappings such as `metadata`, whose entries are not checked; a line that belongs to no entry, or a list or mapping line indented out of place, is an `ERROR` at that line, and a value YAML cannot read, such as an unclosed quote, is one at its key's line. These frontmatter findings, with a missing, unterminated, or unreadable frontmatter and a duplicate key, carry the check `copilot-config`; the rules below carry `copilot-skill` and `copilot-agent`. The frontmatter needs a lowercase hyphenated `name`, a description, and no empty `allowed-tools`, neither an empty value nor an empty list `[]`. Copilot's add-skills page says the name typically matches the skill's directory, not that it must, so a name that differs is no Copilot finding; Codex's rule that it match is below. Custom agents in `.github/agents`, the location Copilot documents, need a `.md` or `.agent.md` filename, a description, `target` of `vscode` or `github-copilot`, boolean switches, `tools` as a flow or block list of names or a comma-separated string, and `modelPolicy` of `preferred` or `required`. GitHub's custom-agents reference enables every tool when `tools` is unset and disables every tool with an empty list `tools: []`, so `[]` passes; an empty value (`tools:` or `tools: ""`) is an `ERROR`, because YAML reads it as unset, which enables every tool its author may have meant to remove. A skill or agent named `code-review` collides with Copilot's built-in agent (`ERROR`). A marker-bearing projection in `.github/skills` or `.github/agents` must be manifest-owned (`provenance`); a manifest-owned one without the marker is the `ownership` finding. A file that cannot be read or decoded, or an agent directory that cannot be listed, is an `ERROR`, never a crash.

A skill or agent is also judged by the rules of every other runtime that reads its folder:

- Skills in `.claude/skills` and `.agents/skills` meet the Agent Skills field requirements (`agent-skill`, `ERROR`): a `name` of at most 64 characters without an XML tag or the reserved words `anthropic` and `claude`, and a description of at most 1,024 characters without an XML tag. The reserved words bind the name only, as Anthropic's pages give them; Copilot's `.github/skills` keep Copilot's rules alone.
- A skill in `.claude/skills` whose body reads `$ARGUMENTS` without an `argument-hint`, which Claude Code shows during autocomplete, is a `claude-skill` `WARNING`.
- A skill in `.agents/skills`, where Codex reads it (`codex-skill`): a `name` that differs from its folder is an `ERROR`, since Codex's skills page builds on the Agent Skills specification, which requires the match, while Claude Code's skills page lets a `.claude/skills` name differ from its folder, so it is no finding there. One that is user-only, by its own `disable-model-invocation: true` or that of the `.claude/skills` skill of the same name, is a `WARNING` unless its `agents/openai.yaml` sets `policy.allow_implicit_invocation: false`, since Codex reads only that file and otherwise invokes the skill on its own; a policy file that cannot be read is an `ERROR`. A description that differs from the `.claude/skills` skill of the same name is a `WARNING`.
- An agent in `.claude/agents` is a Claude Code subagent, read with the sub-agents page's fields (`claude-agent`), and a Copilot CLI custom agent too, since its command reference reads the folder after `.github/agents`: only its `.md` files count; a missing `name` or `description`, and a name over 256 characters, holding `:`, or starting with `-`, which Claude Code skips, are `ERROR` findings, the frontmatter problems above carry this check, and an `INFO` names what the agent may do: its tools, every tool when it names none, its model, and whether it has hooks of its own. Copilot's rules for `.github/agents` above, but for its file name and description, hold it too (`copilot-agent`): its `target`, switches, `tools`, and `modelPolicy`, and a file named `code-review`, the ID Copilot gives it, is a `collision`.

Optional manifest `runtimeRoles` must map `copilot_cli` and `copilot_app` to `full_local_host`, `cloud_agent` to `deferred_remote_worker`, and `code_review` to `advisory_evidence_only`. Missing declarations are a `WARNING`; wrong ones are an `ERROR`. The audit stops at wiring and declared boundaries: it does not run a Copilot session, invoke MCP, or validate review results.

### 9. Behavioral Constraints (`behavioral`)

`WARNING` when `CLAUDE.md` lacks a "never disable" or "never suppress" rule, a rule forbidding workarounds for failing checks, or a quality gate (a coverage threshold, a lint severity level, or a rule that every check must pass). Gate detection is a text heuristic.

**Why**: These guardrails stop agents from weakening the repository's own quality standards.

### 10. Ownership (`ownership`)

Every manifest artifact needs ownership tracking: an embedded `AUTO-GENERATED from CLAUDE.md` marker for comment-supporting files and a manifest hash for JSON files. A readable comment-supporting artifact without the marker is an `ERROR` here, and only here, even when parity also fails for it; a JSON artifact without a hash fails the manifest schema (an `authority` `ERROR`), and a rejected, missing, or unreadable artifact is parity's finding.

**Why**: Without ownership, the generator cannot safely update or delete files, and a forged manifest must not authorize deleting arbitrary files.

### 11. User-Authored Collisions (`collision`)

An unmarked `AGENTS.md` when Codex is targeted, or an unmarked `.github/copilot-instructions.md` when a Copilot surface is targeted, is an `ERROR`: the generator refuses to overwrite it, so it must be resolved deliberately.

### 12. Claude Code Project Configuration (`claude-settings`, `claude-hook`, `claude-memory`)

Claude Code's own project files, read statically as its settings, permissions, hooks, and memory pages describe them:

- `.claude/settings.json`, the settings file the settings page has a team commit, must parse as a JSON object (`claude-settings` `ERROR` otherwise). Its `permissions` apply to everyone who opens the folder, so they are named: `allow` rules or `additionalDirectories`, which grant capability once the workspace is trusted, are a `WARNING` that personal approvals belong in `.claude/settings.local.json` or `~/.claude/settings.json`; other entries, such as `deny` and `ask`, are an `INFO`. A `defaultMode` the settings reference does not list, a list or object among them, is an `ERROR`, and `auto` or `bypassPermissions`, which do not take effect from project settings, a `WARNING`.
- Its `hooks` (`claude-hook`): an event the hooks page does not list is an `ERROR`, since Claude Code fails that entry and its hooks never run, as is a level of the event, matcher group, and handler shape that is not the page's, a handler `type` other than `command`, `http`, `mcp_tool`, `prompt`, or `agent`, a list or object among them, a handler without the field its type requires (`command`, `url`, `server` and `tool`, or `prompt`), a `timeout` that is not a positive number of seconds, and an `onFailure` other than `continue` or `block`, or on a hook other than `command` or `http`. A `command`, `http`, or `mcp_tool` handler without a `timeout` is a `WARNING`: it may run for the 600-second default, except on the events whose default the page makes shorter (`UserPromptSubmit`, `PreModelSwitch`, `PostModelSwitch`, `MessageDisplay`, and `SessionEnd`).
- Each `CLAUDE.md`, `.claude/CLAUDE.md`, and `CLAUDE.local.md`, and each rule (`claude-memory`): one over 200 lines, the memory page's target, is a `WARNING`. A `CLAUDE.md` file's `@path` imports, outside code spans and fenced code blocks, are named in an `INFO`, since imported files load together with the file that imports them and do not reduce its context cost, and a relative import that names a path (one with a folder or extension) resolving inside the repository to no file is a `WARNING`.
- Each rule below `.claude/rules`, found recursively as the memory page says (`claude-memory`): an `INFO` names the globs its `paths` frontmatter list scopes it to, or that it has none and loads at launch; a `paths` value that is not a list of globs is a `WARNING`, and a rule that cannot be read an `ERROR`.

**Why**: These files change what Claude Code loads and may run for everyone who opens the repository, and a hook or rule Claude Code never loads fails silently.

### 13. Codex Project Configuration (`codex-hook`, `codex-agent`, `codex-rule`)

Codex's project files beside `.codex/config.toml`, each loaded once the project's `.codex` layer is trusted, read statically as its hooks, subagents, and rules pages describe them:

- Hooks in `.codex/hooks.json` and in the `[hooks]` tables of `.codex/config.toml` (`codex-hook`): a `hooks.json` that does not parse as a JSON object is an `ERROR`; an event that neither the hooks page nor Codex's release notes list (`PreModelRequest` and `PostModelRequest` arrived in Codex 0.164.0, before the page lists them), which Codex ignores, a level of the shape that is not the page's, a handler `type` other than `command` or `mcp_tool`, which the page says Codex supports, a `prompt` or `agent` hook, which it parses but skips, a `command` hook without `command`, an `mcp_tool` hook without `server` or `tool`, and a `timeout` that is not a positive number of seconds are `ERROR` findings; a `command` or `mcp_tool` hook without a `timeout` is a `WARNING`, since it may run for the 600-second default, except on `SessionEnd` and `Interrupt`; an `async` hook on `PreToolUse`, `PermissionRequest`, `UserPromptSubmit`, or `Stop` is a `WARNING`, since the page says a background hook cannot block or control the operation, so it is no tool policy, permission decision, prompt rejection, or turn continuation; and hooks in both files are a `WARNING`, since Codex merges them and warns at startup.
- Each custom agent, `.codex/agents/*.toml` (`codex-agent`): a file that does not parse is an `ERROR`, as is one without `name`, `description`, or `developer_instructions`; a `sandbox_mode`, `approval_policy`, `model_provider`, or `mcp_servers` in it is a `WARNING`, since the page says those do not override the parent session there.
- Each rules file, `.codex/rules/*.rules` (`codex-rule`), read with Python's parser, which never runs it: a file that does not parse is an `ERROR`, as is a `prefix_rule` without a non-empty literal `pattern` or with a `decision` other than `allow`, `prompt`, or `forbidden`; a `prefix_rule` that allows, as one without a `decision` does, is a `WARNING` at its line, since it runs the command outside the sandbox without prompting for everyone who trusts the project.

**Why**: A committed hook, agent, or rule changes what Codex runs for everyone who trusts the repository.

### 14. Applicable Limitations (`limitation`)

`INFO` findings for each documented limitation this repository hits: marker-only parity for TOML and YAML artifacts, a manifest without `copilot_sections`, provenance-only Copilot projections, unanalyzed path-specific instructions, and nested `.mcp.json` files. They never change the exit code. See `known-limitations.md` for detail.
