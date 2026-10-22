# Known Limitations — audit-ai-config

Claude Code, GitHub Copilot, and Codex each reviewed these limitations against the audit engine,
`scripts/audit_ai_config.py`.

The engine reports each limitation below that applies to the audited repository as an
`INFO` finding with check `limitation`, when it can detect that it applies.

## Contents

- Scope integrity is conditional
- Malformed manifests can produce conforming classification
- Older manifests use default Copilot sections
- Codex TOML and workflow YAML use marker-only parity
- Instruction-layering analysis is incomplete
- Copilot configuration is static, not operational
- Frontmatter is read as simple YAML
- Copilot projections are only provenance-checked
- MCP validation gaps
- Git repository check is CLI-only
- Read-only contract
- Behavioral checks are heuristics
- The MCP handshake covers stdio servers only

## Scope integrity is conditional

The audit now parses literal `TARGET_RUNTIMES`, `TARGET_SURFACES`, and
`TARGET_FEATURES` assignments from a recognized repository generator using Python
AST only; it never imports or executes the generator. A mismatch with the editable
manifest is an error.

This hardening is unavailable when there is no recognized generator, assignments
are dynamic, parsing fails, or target declarations are incomplete. In that case the
report is explicitly `manifest-declared-only` and emits a warning naming the reason:
a structurally valid but narrowed manifest can still suppress checks. When no manifest exists either,
no editable scope exists to narrow, and the report is `file-derived`: the layering checks cover the
runtimes and surfaces the repository's files show it serves, which an informational finding names.
That reading is a heuristic: a `.github` folder stands for Copilot's CLI, app, and code review, though
a repository on GitHub may not use them, and JetBrains and VS Code, which only settings and Copilot's
own files show, are never covered without a manifest. When no file shows a runtime or surface, the
report is `no-declared-scope`. A future independently trusted configuration artifact could close that
remaining gap.

## Malformed manifests can produce conforming classification

Malformed JSON and non-object manifests produce only WARNING findings and do not count as a conforming signal. With two independent authority signals (e.g., `CLAUDE.md` + maintaining section, or `CLAUDE.md` + generator script), the repository classifies as conforming with empty manifest scope. Because the warnings are not ERRORs, the audit can report `RESULT COMPLIANT` (exit code 0), unless a recognized generator declares the scope: that scope cannot be compared with a manifest the audit cannot read, which is one `scope` ERROR.

Separately, structurally valid JSON objects that fail schema validation (e.g., missing required fields, wrong types) produce ERROR findings and force `RESULT ERRORS` (exit code 1), though the conforming classification may still appear if other signals are present.

## Older manifests use default Copilot sections

Current generator manifests record `copilot_sections`, which the audit uses to reconstruct Copilot instruction parity without importing or executing the repository generator. Older schema-version-1 manifests without this optional field fall back to the original four default section names (`Overview`, `Build and Test Commands`, `Formatting Rules`, `CI / Quality Gates`). A customized older generator should regenerate its manifest before relying on this parity check.

## Codex TOML and workflow YAML use marker-only parity

Deterministic reconstruction covers AGENTS.md, skill shims, and copilot-instructions. Codex `config.toml` and workflow YAML artifacts are checked for the ownership marker only — content drift within those files is not detected.

## Instruction-layering analysis is incomplete

Surface resolvers exist for Codex, Copilot CLI/app, JetBrains, cloud agent, code review, and VS Code. Codex nested files use correct additive-chain semantics for `AGENTS.override.md` and `AGENTS.md`; the fallback names a repository's `.codex/config.toml` gives Codex are read for the root only, and the fallback names a user's own Codex configuration gives cannot be read. The size cap is held for the root file only, not for the chain of nested files Codex adds below it, and against the cap the repository's `.codex/config.toml` sets, never one a user's own configuration sets. The engine does not judge whether effective sources contradict or overlap, including path-specific instructions; the skill asks the agent to review that content as a separate, labelled assessment.

## Copilot configuration is static, not operational

The audit statically validates repository custom-agent filenames and selected
frontmatter fields, but it does not validate every environment-specific field,
tool alias, MCP server schema, model entitlement, or runtime-specific parser
behavior. It does not contact GitHub or start a Copilot CLI, app, cloud-agent, or
code-review session.

Repository settings, organization and enterprise policies, authentication, model
availability, runtime enablement, custom-instruction enablement, MCP allowlists,
and actual skill or agent selection remain manual verification items. Copilot code
review's documented PR-head loading is reported as a trust warning, but the audit
cannot determine what a particular historical review actually loaded.

## Frontmatter is read as simple YAML

Skill and agent frontmatter is read line by line so that each finding names its line. Quoted scalars, lists, block
scalars, and scalars continued on indented lines are read as YAML reads them, through the repository's shared reader,
except a block scalar with an explicit indentation indicator or a folded block scalar with more-indented lines, which
the shared reader refuses and the engine reads itself. A nested mapping such as the Agent Skills specification's
`metadata` is accepted without checking its entries. Values are text without YAML type resolution, so a quoted
`"true"` counts as a boolean. A one-line plain value is kept as written, without the trailing ` # comment` YAML ends
it at, even one YAML would refuse, such as `*bold* text`, and a flow list the shared reader cannot read, such as
`[pr-number] [priority]`, is kept as text. A double-quoted value with an escape JSON lacks, such as `\x41`, is
reported as unreadable.

## Copilot projections are only provenance-checked

For manifest-owned or marker-bearing `.github/skills` and `.github/agents`
projections, the audit verifies ownership marker and manifest relationship. It
does not reconstruct a generator-specific projection byte-for-byte because the
generated layout (`generated-layout.md`) defines no Copilot skill/agent
projection template or manifest schema for canonical-source mapping. Such projections remain derived
artifacts rather than authority sources.

## MCP validation gaps

- A missing `mcpServers` wrapper in `.mcp.json` or `.github/mcp.json`, non-object server entries, and non-table
  `.codex/config.toml` server entries produce warnings, not errors; a missing `servers` wrapper in `.vscode/mcp.json`
  is an error. The audit and the handshake read each file through one reader, so they report the same problems.
- VS Code and Codex server definitions receive minimal structural validation beyond container type checks.
- Ancestor-chain `.mcp.json` discovery is not implemented; nested `.mcp.json` files are reported as a limitation but not validated.
- Cross-runtime parity does not cover `env_vars`, HTTP headers, or authentication fields.
- `check_mcp` does not re-guard MCP server fields after schema validation (relies on schema having caught type issues).

## Git repository check is CLI-only

The CLI checks `args.root / ".git"` exists (not equivalent to `git rev-parse --show-toplevel`). The `audit()` library function does not enforce repository identity.

## Read-only contract

The audit engine is verified read-only: no writes, no execution, no modifications, no subprocess, no tempfile, no network access. All file access uses `read_text()`, `exists()`, `is_file()`, `is_dir()`, `resolve()`, `iterdir()`, `glob()`, `rglob()`, `os.walk()`, `json.loads()`, and `tomllib.load()` on a file opened in read-binary mode.

## Behavioral checks are heuristics

The engine checks suppression language, workaround rules, and quality-gate statements (a coverage threshold, a lint severity level, or a rule that every check must pass). Quality-gate detection is a text heuristic and can miss gates phrased in other ways. Whether a repository needs a dogfooding requirement is a judgment the skill leaves to the agent.

## The MCP handshake covers stdio servers only

`scripts/mcp_handshake.py` starts stdio and `local` servers only and skips remote transports, which would need network access and often credentials. It starts each server as a client does: a `command` with a directory part, such as `./server.cmd`, is resolved against the configured `cwd` under the repository root, and a bare name is looked up on `PATH`. A `cwd` that leads out of the root, through `..`, an absolute path, or a link, and a relative command that does, are refused with `CONFIG_ERROR <file> Server '<name>': cwd '<cwd>' is outside the repository` (or `command '<command>' is outside the repository`) in each file that declares the server, which is then `SKIPPED`; an absolute command runs as configured. Every name, command, type, and reason it prints is flattened onto its line. It passes configured `env` values literally, so placeholders such as `${input:token}` or `${env:NAME}` are not expanded and can make a server fail its handshake. It never calls a tool, so a server that fails only on tool invocation still passes. It checks the shape of the initialize and `tools/list` results against the MCP schema and accepts only the protocol revisions 2024-11-05, 2025-03-26, and 2025-06-18; the script requests 2025-06-18, so only a server that no longer supports it and answers with a later revision fails. `--timeout` bounds each server from its start to its stop, but stopping a server ends only the process its command started: a program that a launcher such as a `.cmd` file starts and leaves running is not stopped, though the run no longer waits for it.
