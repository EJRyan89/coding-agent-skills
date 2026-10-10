---
name: skill-core
description: "Internal Python modules shared by other skills' scripts. Not intended for direct invocation."
disable-model-invocation: true
user-invocable: false
---

# Skill core

This is a non-selectable dependency. Its `${CLAUDE_SKILL_DIR}/scripts/` folder holds Python modules that other skills' scripts import, such as `console.py`, which sets UTF-8 output for an entry point, `frontmatter.py`, which reads a SKILL.md or agent definition's frontmatter, `github_client.py`, the GitHub CLI client every `gh` call goes through, `git_client.py`, the git client, both bounded and kept from prompting by `bounded_process.py`, `skill_roots.py`, the directories the deployer installs skills in, and `flat_text.py`, which flattens free text onto one line. It has no workflow to run.
