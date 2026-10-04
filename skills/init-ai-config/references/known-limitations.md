# Known Limitations — init-ai-config

Reviewed 2026-09-18 during public packaging; updated 2026-10-02.

## Write-mode manifest trust boundary

`--write` uses manifest hashes as JSON ownership proof only when the manifest matches the valid manifest committed at Git `HEAD`. An editable working-tree manifest cannot authenticate its own hashes. A first uncommitted generation can be repeated when every existing artifact exactly matches the newly expected output, but scope or generator changes that would replace JSON require the prior generated state to be reviewed and committed first.

This deliberately treats reviewed Git history as the trust anchor. A malicious manifest already accepted into `HEAD` is trusted, so repository review and branch protection remain part of the security boundary.

## Cleanup is preflighted but not atomic

Cleanup conflicts are computed before any writes or deletions. Conflicts abort the entire operation. However, if a deletion succeeds but a later write fails, the deletion is not rolled back. True atomicity requires temporary staging.

## Cleanup reconstruction uses current sources

Scope-narrowing cleanup reconstructs expected artifact content from the current `CLAUDE.md` and skill frontmatter. If the canonical source changes simultaneously with scope narrowing, unmodified generated files may be falsely classified as modified and reported as conflicts.

Codex `config.toml` artifacts cannot be automatically cleaned because no deterministic reconstruction is available for them. The CI parity workflow and its pull-request caller have fixed content and are cleaned like other generated files.

## MCP server isolation is limited by platform behavior

Root `.mcp.json` is readable by both Claude Code and Copilot CLI/app. A Claude-only server in `.mcp.json` is visible to Copilot. The generator produces a non-blocking warning but cannot prevent this platform behavior. `copilot_local` and `copilot_repository` scope consistency is not checked — a server targeting these without corresponding Copilot surfaces is not flagged.

## Invalid manifest paths are rejected without diagnostics

`load_manifest()` rejects manifests containing unsafe artifact paths (traversal, backslash, outside allowlist) by returning `None`. No diagnostic identifies which path caused the rejection. The caller sees the manifest as absent or invalid with no further detail.

## CI caller detection uses indentation heuristics

`find_ci_parity_callers` distinguishes job-level `uses:` from step-level `uses:` by indentation depth. Compact YAML with unusual indentation can produce false positives. Reliable detection requires YAML parsing. The generated workflow also lacks MCP smoke tests and does not verify trigger reachability.

## CLI overrides are invocation-local

`--runtimes` and `--surfaces` CLI arguments are not persisted. Subsequent `--check` without those arguments uses `TARGET_*` constants and may report stale config if the manifest was generated with different scope.

## Codex config.toml is incomplete

`env_vars`, HTTP authentication fields, and Codex policy fields (`approval_mode`, `enabled_tools`, `disabled_tools`) are accepted in the spec but not yet emitted by the generator. Unique server name validation is not enforced at the TOML level.

## VS Code and Codex MCP fields not validated by --validate-config

`--validate-config` validates MCP server structure and transport/target compatibility but does not check runtime-specific field requirements for VS Code or Codex TOML output.

## Git repository check is CLI-only

Both CLIs check `args.root / ".git"` exists, which is not equivalent to `git rev-parse --show-toplevel`. Library functions (`regenerate`, `validate`, `expected_generated_files`) do not enforce repository identity.

## The spec owns the generator constants

`install` rewrites every repository-specific constant in `.github/scripts/ai_config.py` from the spec, so a constant edited by hand is replaced on the next install unless the spec was exported from the edited generator first. `export-spec` reads literal assignments only; a computed constant fails the export rather than being guessed, and constants an older generator lacks take the template defaults and are reported as `DEFAULTED`.

## Detection is a starting point

`detect` lists build and formatter files by name at most two directories deep, skipping hidden directories and common dependency or output directories such as `node_modules`, `bin`, and `obj`. It infers an MCP server's transport from its `type`, else `command` (stdio), else `url` (http). The agent still reads the listed files to learn the actual commands.
