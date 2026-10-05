# Runtime checks

These checks need a running Codex or Copilot session, so the user confirms them after `init-ai-config` finishes. Each section covers one target; only the targets selected for the repository apply.

## Codex

Instructions load from the repository root and from a nested directory; `CLAUDE.md` is reached through the `AGENTS.md` adapter; repository skills appear through `.agents/skills`; `codex mcp list` shows the expected servers and each completes a transport-appropriate smoke test; behavior is documented for an untrusted project and for a missing server executable.

## Copilot CLI/app

`.github/copilot-instructions.md` loads as repository instructions without excessive context; path-specific instructions do not conflict with it; repository skills and file-based MCP servers are discovered.

## VS Code

MCP servers are discovered from `.vscode/mcp.json`, and the CLI does not read that file.

## Cloud agent

`copilot-setup-steps.yml` provisions the expected environment; repository MCP settings carry tool allowlists; secrets for authenticated servers are documented, including behavior when they are missing.

## Code review

Once custom instructions are enabled, repository-wide and path-specific instructions are respected.
