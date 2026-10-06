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

The safety invariants these scripts keep, including the Copilot CLI host's isolation, are in "Safety invariants" of `docs/code-review-operations-contract.md` in this suite's source repository. Never invoke Copilot directly; `dispatch` starts the bounded host in `${CLAUDE_SKILL_DIR}/scripts/review_hosts.py`.
