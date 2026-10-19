---
name: implement-change
description: >-
  Procedure for making a change in a repository that publishes an implementation profile
  (docs/implementing-changes.md), from the worktree and the plan to the pull request. Use it when asked to implement
  an issue or make a change in such a repository.
allowed-tools: ["Bash(python -B .claude/skills/implement-change/scripts/pull_request.py *)", "PowerShell(python -B .claude/skills/implement-change/scripts/pull_request.py *)"]
---

Read and follow `../../../.claude/skills/implement-change/SKILL.md`, a path relative to this file, as the authoritative workflow.
Run its commands from the repository root, the folder that holds `.agents` and `.claude`, and resolve every relative path it names from there.
A command that writes a shim under `.agents` needs an approval to run outside Codex's sandbox, which keeps `.agents` read-only.
