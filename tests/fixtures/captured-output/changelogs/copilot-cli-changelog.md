## 1.0.103 - 2026-10-22

- Clicking a completed response code block copies its original code when enabled.
- ACP sessions now honor saved sandbox settings and --sandbox/--no-sandbox flags, including on resume.
- Cloud replies keep full text when the CLI reconnects after a dropped connection
- Sessions browser shows a session's recorded title instead of a generic Session N fallback when available
- Model-specific default Responses verbosity now applies correctly for GPT-5 models unless session verbosity is explicitly set
- Allow dnx commands to use existing .NET and NuGet sandbox grants when developer-tool access is enabled
- Windows dev-tool shims like npm.cmd now receive the same sandbox grants as npm.
- Gemini tool calls now avoid 400 schema validation errors for nullable and enum fields
- Resume sessions under mandatory managed model defaults without rejecting saved preferences
- Hash mention suggestions refresh automatically when the GitHub client becomes ready
- Reuse cached local MCP tool snapshots across registered Git worktrees in one repository
- Improve remote launch options with clearer hosting/connection flags and retire legacy --remote/--no-remote startup flags

## 1.0.102 - 2026-10-21

- Prompt mode now honors managed Auto model selection for -p and piped stdin runs
- Subagents honor custom and MCP tool approvals, avoiding duplicate permission prompts and allowing inherited workflows to run
- Plugin marketplaces accept git-subdir sources and long plugin descriptions
- Fish shell completion works when aliases include option aliases like --effort
- Chats no longer get blocked by organization plugin preparation while sending messages
- Long-running sandboxed commands continue running reliably past 10 seconds
- Replies cut off by a provider overload error retry with backoff and show the provider's message and request ID
- Sandboxed network traffic now honors NO_PROXY and ALL_PROXY when inheriting host proxy settings

## 1.0.101 - 2026-10-20

- Add native structured output for dynamic workflow subagents on supported models
- Add assistedApprovalInstructions setting to guide Assisted Permissions confirmation decisions
- Shell commands that create and then enter a directory now request path approval instead of being denied early.
- Headless and SDK sessions without an OAuth handler skip MCP connections when saved tokens require sign-in, while renewable grants still connect.
- macOS sandboxed commands can connect directly to localhost TCP services when local access is enabled and no host filters apply.
- Local models and loopback providers in providers.json no longer block Auto, your saved model or the default model when signed in to Copilot
- Explicit Entra MCP sign-in now recovers from rejected cached tokens by continuing to interactive sign-in.
- Switching only reasoning effort no longer forces compaction in over-budget sessions.
- Copy keys now reliably copy mouse selections even when pressed immediately after dragging.
- Cancel abandoned Microsoft sign-in flows so retries start cleanly
- Configured MCP inventory reads no longer fail when startup state is unchanged
- Google models now avoid repeated request rewrites when screenshot-heavy sessions exceed image limits, reducing extra token usage.
- Install Team-assigned plugins from Team-defined GitHub marketplaces
- Extension capability requests use explicit approval instead of the Assisted judge
- Shared MCP auth errors point to your client's sign-in settings; CLI authentication warnings still show /mcp auth.
- Switching from GPT models to Gemini no longer fails when earlier turns include custom tool calls
- Google model requests now respect registry image limits to avoid image-count request failures
- The first prompt no longer hangs silently when no model is selected at startup; the CLI now asks you to pick one with /model and keeps your prompt
- /usage now shows just the model name for BYOK and other rows without a provider label instead of "Unknown provider"
- Cancelling an admission-gated prompt now re-enables Enter without requiring extra typing while background work continues.
- Model picker marks your administrator-managed model as the default when it is available
- Preserve MCP PNG images that include ICC color profiles instead of omitting them
- Windows shell commands start reliably in restored sessions and clean up orphaned child processes
- Switching to HydraFusion compacts only when the routed model actually needs it
- CLI model lists refresh after endpoint updates instead of staying stale or empty
- Windows taskbar icon staging avoids concurrent temp-file collisions so icon presence remains stable across launches.
- Resuming a session shows the conversation sooner without waiting for placeholder environment setup
- Starting a session with sandboxed MCP servers now avoids unnecessary restarts and responds faster.
- Allow /config and /settings to set and unset showReasoning
- Permission approval prompts explain escalation causes and show a request-specific diagnosis command

## 1.0.100 - 2026-10-19

- Add --disable-client-telemetry flag to turn off client telemetry uploads from the CLI
- Add --trust-folder for one-off prompt-mode sessions started with -p or piped stdin
- Install verified Skills and plugins from GitHub install links via copilot plugin install "ghapp://air/install?..."
- Open /share without arguments to view a full-screen action menu for sharing the current session
- Sandbox host-rule traffic now honors HTTPS_PROXY and HTTP_PROXY when no sandbox proxy is configured
- Voice dictation no longer inserts language markers like <en-US> into transcript text
- Managed plugins update activation in existing sessions when managed policy changes
- Sessions no longer stall for minutes when an MCP server hangs while loading tools.
- Gemini tool calls no longer fail when array parameters define enum values
- Gemini chats no longer fail after earlier tool results contain no readable content.
- Sandboxed shell commands now explain credential-proxy certificate trust failures and can offer an approved rerun outside the sandbox.
- Keep saved /diff review comments when reopening an unchanged diff in the same session, with an explicit discard action.
- Plugin enable, disable, update, and uninstall actions now route reliably after plugin inventory refreshes.
- Reasoning-only max-token responses no longer loop indefinitely and now end with a clear error when no progress is made
- Show a clear warning and keep the active model display in sync when a saved model is automatically replaced on resume.
- Sandboxed commands on Linux start more reliably without intermittent loopback setup failures
- Back from MCP resource details stays on the list during live metadata refreshes
- Native chats can start and finish while optional managed plugin downloads continue in the background.
- Double Ctrl+C exits immediately even when the CLI UI is busy
- Catalog search reports proxy authentication requirements for CONNECT proxy challenges
- HydraFusion keeps a pass expanded while its review runs, passes that were not kept stay expandable until you switch sessions or resume, and passes from an aborted turn say whether they were kept
- Managed account policy and bypass-mode warnings stay accurate after switching accounts, even when a policy cache miss happens.
- Avoid false context exhaustion and rollover refusal when tool definitions are deferred
- Custom agents that declare the documented `search`, `web` or `todo` tool aliases, or name `github-mcp-server/web_search`, now receive the matching tools
- CLI help and version flags exit reliably after printing their output
- Enterprise-managed ask permissions in Assisted mode now allow safe actions without unnecessary confirmation prompts.
- Keep your selected Microsoft Entra account pinned during sign-in and token renewal
- Up from sessions sidebar header actions wraps to the last visible session
- Plan, autopilot, and shell mode badges stay visible while startup components load and the agent works. The badge replaces the mode suffix that used to trail the activity text (`Working - plan`), and the startup `Loading:` row now truncates its diagnostics to keep the status row on one line
- Sandboxed commands can write package and build caches when developer tool access is enabled.
- Windows CLI Git commands now recover from thread-access-denied startup failures.
- A skill `allowed-tools` rule keeps commas inside its arguments instead of being split apart. Where a list's structure is ambiguous, its unscoped rules now prompt instead of being granted.
- MCP resource dashboard stays interactive while inventory adapters refresh
- Retry image-bearing Responses requests with smaller payload limits when providers reject oversized bodies
- Complete usage reporting no longer shows partial status or phantom unattributed spend after resume when billing data is complete.
- Assisted permissions send nested PowerShell and bash -c commands that only expand plain-data environment variables, single-quoted PowerShell, and PowerShell variable and alias statements to the permission judge instead of requiring unnecessary manual approval
- Path-specific instruction files in the repository root .github/instructions are now applied automatically when the agent reads or edits a matching file
- Start git, shell commands, and MCP servers on Windows when process snapshot thread enumeration fails
- Use the unified Sessions browser by default for /sessions, /session, and /resume.
- Session browsing now caps account discovery at 500 records and respects Retry-After hints for automatic refresh timing.
- Prompt to update the sandbox proxy certificate authority when masked hosts change
- Show a clear notice when administrator-managed settings override your selected model
- Enable semantic code search by default for online sessions.
- Improve /login for Microsoft Entra by letting you choose a cached account or explicitly sign in with a different account
- Sandboxed git on github.com can use your configured GITHUB_TOKEN from credentials.envVars
- /sandbox can keep sandboxing enabled for the current session without enabling it globally
- Trusted repository plugins install and update automatically before chat runs.
- Rename the built-in browser plugin to browser-use and keep existing browser plugin preferences working after upgrade
- Sandbox allows local network access by default so commands can reach localhost and private-network services.

