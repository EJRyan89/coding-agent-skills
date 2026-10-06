---
name: code-review-core
description: Internal support for the code-review operations bundle. Not intended for direct invocation.
allowed-tools: ["Bash(python -B \"${CLAUDE_SKILL_DIR}/scripts/*)", "PowerShell(python -B \"${CLAUDE_SKILL_DIR}/scripts/*)", "Read"]
disable-model-invocation: true
user-invocable: false
---

# Code-review core

This is a non-selectable dependency of the public code-review operations skills. Do not invoke it as a user workflow.

Its scripts own deterministic configuration, state, GitHub enumeration, review-record, archive, flag, and runtime-adapter behavior. Public skills provide the agent-facing orchestration and must treat validated JSON records as the machine source of truth. `${CLAUDE_SKILL_DIR}/scripts/review_pipeline.py` is the only review entry point an orchestrating agent runs: `enumerate`, `prepare`, `dispatch`, `wait`, `check`, `finalize`, and `advance` cover every step except starting reviewer subagents. Reviewers run `validate-result` on their own result before replying; the orchestrator never runs it, and `check` stays authoritative.

Before changing a persisted format (configuration, reviewer manifests, adapter request, review record, flag store, or legacy index), read its table in the "Formats" section of `docs/code-review-operations-contract.md` in this suite's source repository. Before changing the adapter result, read `${CLAUDE_SKILL_DIR}/references/review-adapter.schema.json`. Unknown future schema or protocol versions fail closed.

Safety invariants:

- Configuration selects symbolic runtime and reviewer identifiers, never executable commands.
- Repository identities are always full `owner/repo` values.
- Mutable files use validated temporary writes and atomic replacement.
- Review records are written as linked JSON/Markdown pairs under collision-safe owner/repository paths.
- Runtime output is untrusted until it satisfies the adapter-result schema.
- Review, re-review, tracker, flag, and insight operations only read GitHub state. None of them posts comments, creates pending reviews, or submits review state.
- Repository-provided reviewers are loaded only from an immutable trusted commit.
- Review source is materialized from the exact PR head into a hash-verified snapshot. Agent configuration and instruction paths from the PR head are excluded; only reviewer-manifest files from the trusted base may instruct the reviewer.
- The suite-owned generic reviewer and result schema are resolved in this installed skill's `${CLAUDE_SKILL_DIR}/references/` directory. They never depend on a configured repository checkout, its current branch, or a hard-coded main-worktree path.

When configuration selects `copilot-cli`, use the bounded host in `${CLAUDE_SKILL_DIR}/scripts/review_hosts.py`; do not invoke Copilot directly. The host requires Copilot CLI 1.0.88 or newer, verifies the exact file set and hashes of every materialized reviewer resource, requires the diff and source snapshot to be inside the isolated run directory, runs with isolated `HOME`, `USERPROFILE`, `COPILOT_HOME`, and working directory values, disables ambient instructions and MCP servers, and grants write access only to its attempt's staging file, which the host renames to the result path once it is a JSON object and the role has not been set aside. `dispatch` starts the host detached and `wait` follows it, so no runtime's command time limit stops a review. Authentication tokens inherited from the invoking environment remain available by design, so treat this as configuration isolation rather than credential isolation.
