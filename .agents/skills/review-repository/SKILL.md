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

Read and follow `../../../.claude/skills/review-repository/SKILL.md` as the authoritative workflow.
Resolve all relative paths and supporting resources from `../../../.claude/skills/review-repository/`.
