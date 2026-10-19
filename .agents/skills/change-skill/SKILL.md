---
name: change-skill
description: >-
  Procedure for adding or changing a skill in this repository, shipped under skills/ or repository-only under
  .claude/skills: its SKILL.md, scripts, metadata, or documentation. Use it when asked to add, create, change, fix,
  or extend a skill here. Not for deployer-only, repository-tool, or documentation-only changes.
allowed-tools: ["Bash(python -B .claude/skills/change-skill/scripts/change_skill.py *)", "PowerShell(python -B .claude/skills/change-skill/scripts/change_skill.py *)", "Bash(python tools/new_skill.py *)", "PowerShell(python tools/new_skill.py *)", "Bash(python tools/skill_shims.py --write)", "PowerShell(python tools/skill_shims.py --write)", "Bash(python tools/skill_reference.py --write)", "PowerShell(python tools/skill_reference.py --write)", "Bash(python tools/skill_reference.py)", "PowerShell(python tools/skill_reference.py)", "Bash(python tools/runtime_prompts.py --write)", "PowerShell(python tools/runtime_prompts.py --write)", "Bash(python -B tests/run_validation.py *)", "PowerShell(python -B tests/run_validation.py *)"]
---

Read and follow `../../../.claude/skills/change-skill/SKILL.md`, a path relative to this file, as the authoritative workflow.
Run its commands from the repository root, the folder that holds `.agents` and `.claude`, and resolve every relative path it names from there.
A command that writes a shim under `.agents` needs an approval to run outside Codex's sandbox, which keeps `.agents` read-only.
