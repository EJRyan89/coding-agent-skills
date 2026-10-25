---
name: ejryan89-skill-core
description: "Internal Python modules shared by other skills' scripts. Not intended for direct invocation."
disable-model-invocation: true
user-invocable: false
---

# Skill core

This is a non-selectable dependency. Its `${CLAUDE_SKILL_DIR}/scripts/` folder holds Python modules that other skills' scripts import, such as `console.py`, whose runner starts every entry point with UTF-8 output and ends an unhandled failure with a `FAILED` line, `frontmatter.py`, which reads a SKILL.md or agent definition's frontmatter, `github_client.py`, the GitHub CLI client every `gh` call goes through, `git_client.py`, the git client, both bounded and kept from prompting by `bounded_process.py`, `skill_roots.py`, the directories the deployer installs skills in and the lookup of a sibling skill's scripts that every dependent puts on `sys.path`, `flat_text.py`, which flattens free text onto one line, `folder_walk.py`, the one walk of a repository's folders for files by name, which enters no junction or symbolic link its caller does not follow and skips one stated set of folders and every nested checkout, `diff_paths.py`, which decodes the paths a git diff's file headers name, `typed_fields.py`, the reader of a JSON field's kind that the code-review pipeline's and the pull request tracker's validators and the GitHub activity report share, `whole_number.py`, the one reader of a whole number from text, which takes ASCII digits alone up to the digit limit `json_reader.py` also holds, `versions.py`, the one reader of a version number, which compares versions by their numbers, `path_keys.py`, the one form a path is compared in, `json_writer.py`, the one writer of JSON for a file, which refuses at the write a non-finite number, a lone surrogate, and what `json_reader.py` would refuse reading it back, and `xml_reader.py`, which parses untrusted XML into an element tree after refusing a `DOCTYPE` or `ENTITY` declaration. It has no workflow to run.
