---
name: evaluate-skill
description: >-
  Run a skill's fixed scenarios once per reviewer model from throwaway homes and judge each review record by script.
  Use it before a pull request that changes a skill's prompt, model guidance, or reviewer instructions. Each run
  calls models.
argument-hint: "SKILL [--scenario NAME ...] [--model haiku|sonnet|opus ...]"
allowed-tools: ["Bash(python -B tools/skill_evals.py*)", "PowerShell(python -B tools/skill_evals.py*)"]
---

Read and follow `../../../.claude/skills/evaluate-skill/SKILL.md` as the authoritative workflow.
Resolve all relative paths and supporting resources from `../../../.claude/skills/evaluate-skill/`.
