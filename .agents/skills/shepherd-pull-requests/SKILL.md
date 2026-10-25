---
name: shepherd-pull-requests
description: "Run one merge-shepherd tick from the hub over this repository's open pull requests: fast-forward main, arm auto-merge, merge main into behind branches, judge each red Validation check as a rerun or a defect, review each head report-only once, and name each merged worktree to retire."
argument-hint: "[--dry-run]"
allowed-tools: ["Bash(python -B .claude/skills/shepherd-pull-requests/scripts/shepherd_pull_requests.py *)", "PowerShell(python -B .claude/skills/shepherd-pull-requests/scripts/shepherd_pull_requests.py *)", "Bash(gh pr comment *)", "PowerShell(gh pr comment *)", "Skill"]
disable-model-invocation: true
---

Read and follow `../../../.claude/skills/shepherd-pull-requests/SKILL.md`, a path relative to this file, as the authoritative workflow.
Run its commands from the repository root, the folder that holds `.agents` and `.claude`, and resolve every relative path it names from there.
A command that writes a shim under `.agents` needs an approval to run outside Codex's sandbox, which keeps `.agents` read-only.
