---
name: evaluate-skill
description: >-
  Run a skill's fixed scenarios on each model from throwaway homes, once or several times, and judge each record by
  script: a review's, or whether a request started the skill. Use it before a pull request that changes a skill's
  prompt, description, model guidance, or reviewer instructions. Each run calls models.
argument-hint: "SKILL [--runs N] [--scenario NAME ...] [--model NAME ...]"
allowed-tools: ["Bash(python -B tools/skill_evals.py *)", "PowerShell(python -B tools/skill_evals.py *)"]
---

Read and follow `../../../.claude/skills/evaluate-skill/SKILL.md`, a path relative to this file, as the authoritative workflow.
Run its commands from the repository root, the folder that holds `.agents` and `.claude`, and resolve every relative path it names from there.
A command that writes a shim under `.agents` needs an approval to run outside Codex's sandbox, which keeps `.agents` read-only.
