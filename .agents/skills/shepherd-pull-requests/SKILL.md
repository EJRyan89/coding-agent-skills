---
name: shepherd-pull-requests
description: "Run one merge-shepherd tick from the hub over this repository's open pull requests: fast-forward main, arm auto-merge, merge main into behind branches, judge each red Validation check as a rerun or a defect, and name each merged worktree to retire."
argument-hint: "[--dry-run]"
allowed-tools: ["Bash(python -B .claude/skills/shepherd-pull-requests/scripts/shepherd_pull_requests.py *)", "PowerShell(python -B .claude/skills/shepherd-pull-requests/scripts/shepherd_pull_requests.py *)"]
disable-model-invocation: true
---

Read and follow `../../../.claude/skills/shepherd-pull-requests/SKILL.md` as the authoritative workflow.
Resolve all relative paths and supporting resources from `../../../.claude/skills/shepherd-pull-requests/`.
