---
name: runtime-canary
description: >-
  Check that Claude Code, Codex, and Copilot CLI find and run deployed skills, by deploying this checkout into a
  throwaway home and starting each runtime there. Use it before a pull request that changes skill paths,
  allowed-tools, runtime adapters, or agents. Each run calls a model.
allowed-tools: ["Bash(python -B tools/runtime_canary.py *)", "PowerShell(python -B tools/runtime_canary.py *)"]
---

Read and follow `../../../.claude/skills/runtime-canary/SKILL.md`, a path relative to this file, as the authoritative workflow.
Run its commands from the repository root, the folder that holds `.agents` and `.claude`, and resolve every relative path it names from there.
A command that writes a shim under `.agents` needs an approval to run outside Codex's sandbox, which keeps `.agents` read-only.
