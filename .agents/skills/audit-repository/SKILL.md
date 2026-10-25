---
name: audit-repository
description: >-
  Run this repository's audits per release: before tagging, the release audit of the diff since the last tag and
  the twin sweep of each fix since it; after it, the rotation audit of one area read whole and the guidance audit
  of each runtime's published guidance. Writes the briefs for read-only readers, then files what they confirm.
argument-hint: "release [--since REF] | twins [--since REF] | rotation [--record TAG] | guidance [--record TAG]"
disable-model-invocation: true
allowed-tools: ["Bash(python -B .claude/skills/audit-repository/scripts/audit_repository.py *)", "PowerShell(python -B .claude/skills/audit-repository/scripts/audit_repository.py *)"]
---

Read and follow `../../../.claude/skills/audit-repository/SKILL.md`, a path relative to this file, as the authoritative workflow.
Run its commands from the repository root, the folder that holds `.agents` and `.claude`, and resolve every relative path it names from there.
A command that writes a shim under `.agents` needs an approval to run outside Codex's sandbox, which keeps `.agents` read-only.
