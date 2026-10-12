---
name: skill-core
description: "Internal Python modules shared by other skills' scripts. Not intended for direct invocation."
disable-model-invocation: true
user-invocable: false
---

# Skill core

This is a non-selectable dependency. Its `${CLAUDE_SKILL_DIR}/scripts/` folder holds Python modules that other skills' scripts import, such as `console.py`, whose runner starts every entry point with UTF-8 output and ends an unhandled failure with a `FAILED` line, `frontmatter.py`, which reads a SKILL.md or agent definition's frontmatter, `github_client.py`, the GitHub CLI client every `gh` call goes through, `git_client.py`, the git client, both bounded and kept from prompting by `bounded_process.py`, `skill_roots.py`, the directories the deployer installs skills in, `flat_text.py`, which flattens free text onto one line, `typed_fields.py`, the one reader of a JSON field's kind for every validator, `xml_reader.py`, which parses untrusted XML into an element tree after refusing a `DOCTYPE` or `ENTITY` declaration, and `git_test_base.py`, the test base that isolates every regression suite's git from the shell that started it. It has no workflow to run.
