---
name: review-repository
description: >-
  This repository's own pull request reviewer: a specialists manifest that routes the deployer, the skills, the
  validation policies, the trust-boundary files, and the documents to briefed reviewers, which review-prs runs. Set
  it up on a machine, check it, or tune it from canary reviews.
argument-hint: "[PULL_NUMBER ...]"
disable-model-invocation: true
allowed-tools: ["Bash(python -B skills/code-review-core/scripts/review_pipeline.py validate-reviewer *)", "PowerShell(python -B skills/code-review-core/scripts/review_pipeline.py validate-reviewer *)"]
---

Read and follow `../../../.claude/skills/review-repository/SKILL.md`, a path relative to this file, as the authoritative workflow.
Run its commands from the repository root, the folder that holds `.agents` and `.claude`, and resolve every relative path it names from there.
A command that writes a shim under `.agents` needs an approval to run outside Codex's sandbox, which keeps `.agents` read-only.
