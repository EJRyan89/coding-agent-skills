---
name: audit-repository
description: >-
  Run this repository's two audits per release: the release audit of the diff since the last tag, before tagging,
  and the rotation audit of one area read whole, after it. Writes one brief per area for read-only readers, then
  files what they confirm.
argument-hint: "release [--since REF] | rotation [--record TAG]"
disable-model-invocation: true
allowed-tools: ["Bash(python -B .claude/skills/audit-repository/scripts/audit_repository.py *)", "PowerShell(python -B .claude/skills/audit-repository/scripts/audit_repository.py *)"]
---

Read and follow `../../../.claude/skills/audit-repository/SKILL.md` as the authoritative workflow.
Resolve all relative paths and supporting resources from `../../../.claude/skills/audit-repository/`.
