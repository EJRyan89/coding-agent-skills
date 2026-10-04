# Release checklist

This checklist records the steps already taken to prepare the project for publication, and lists the steps still open before it is published and tagged as `v0.1.0`. Checked items are complete.

## Before the initial commit

- [x] Add the initial reviewed skill set: `dotnet-format`, `repo-cleanup`, `analyze-skill-cost`, `audit-ai-config`, `init-ai-config`, `curate-agent-memory`, `github-activity-report`, and the `code-review-operations` bundle.
- [x] Pass the complete regression suite, skill-specific tests, and static shell checks.
- [x] Complete a clean deployment using an isolated temporary home directory.
- [x] Re-verify that no personal paths, internal organization names, credentials, or generated deployer state are present after the final pre-initialization changes.
- [x] Review the public documentation from a new-user perspective.
- [x] Add clone, first-install, update, and uninstall instructions using the final repository URL.
- [x] Select and add an MIT license.
- [x] Add contributor guidance and a private vulnerability-reporting policy.
- [x] Confirm source ID `ejryan89/coding-agent-skills` and GitHub repository name `coding-agent-skills`.
- [x] Pin GitHub Actions to reviewed commit SHAs and Chocolatey tools to reviewed versions, with an intentional update process.
- [x] Rerun a baseline dedicated secret scan against the complete pre-initialization file set; treat the CI private-reference check only as defense in depth.

## Initial commit and publication

- [x] Remove ignored local build, test, Python-cache, and IDE artifacts (`.vs/`, `bin/`, `obj/`, `TestResults/`, and `__pycache__/`) so they cannot obscure the initial review.
- [x] Initialize the Git repository.
- [x] Run `init-ai-config` against the initialized repository and decide whether to adopt its generated parity pipeline before the initial commit. (Not adopted; rationale recorded in `CLAUDE.md` under "Maintaining AI Agent Config".)
- [x] After the `init-ai-config` decision and any resulting file changes, rerun the complete regression suite, skill-specific tests, and static shell checks.
- [x] Replace the Bash deployer and .NET validation bridge with the Python deployer and parallel `tests/run_validation.py` runner, then re-verify the result: full validation, independent review, an isolated-home deployment of every bundled skill, and fresh private-reference and dedicated secret scans of every tracked file.
- [x] Run a dedicated secret scanner against every final file immediately before the initial commit.
- [x] Review the complete initial diff.
- [x] Create the initial commit.
- [ ] Immediately before publishing, scan for internal repository names, organization names, and paths from a private list kept outside the repository, one per line: `git grep -n -i -F -f <list>` for the tracked tree, and `git log --all -p | grep -n -i -F -f <list>` for history, which publishing also exposes. The CI private-reference check matches only generic patterns, and adding a specific name to it would publish the name.
- [ ] Create and connect the GitHub repository.
- [ ] Push the default branch and confirm validation succeeds.
- [ ] Enable GitHub secret scanning and push protection when available for the repository.
- [ ] As a separate release activity, test installation from a fresh clone in a clean Windows environment, following the README as written: once with only Claude Code installed, once with only Codex CLI, and once with only Copilot CLI.
- [ ] After the Codex-only and Copilot-only installs, run `python deploy.py verify` and confirm it reports every adapter `FOUND` for the installed runtime. In each of the three installs, start `update-coding-agent-skills` from the runtime and confirm it reports `UP_TO_DATE`.

  Both clean-machine items above are deferred for `v0.1.0`: no install on a clean Windows environment has been run by hand. The manual workflow from [issue #12](https://github.com/EJRyan89/coding-agent-skills/issues/12) covers the deployer and runtime-discovery part on a fresh GitHub-hosted Windows runner. A model running a skill, and the Codex Windows sandbox and Python alias settings, stay tested by hand on the maintainer's machines.
- [ ] When tagging, put `SECURITY.md` in the present tense. "Before the first tagged release" and "After the repository is published" in its supported-versions and reporting sections, and "after publication" in its last line, describe a state that has passed once the repository is public and tagged.
- [ ] Tag the first release as `v0.1.0`.

## Code-review operations cutover

These steps are deliberately separate from publishing the base repository and must use copied data or explicitly selected repositories until the final cutover.

- [x] Run the zero-AI Copilot discovery check and a fixture skill/resource-loading canary with an isolated home; confirm the effective source, path, enabled state, and bundled resource access.
- [x] Run the sanitized review fixture through Claude Code, Codex, and GitHub Copilot CLI and validate all normalized results. For Copilot, confirm the isolated host ignores ambient instructions, personal skills, MCP servers, and prompts while writing only the protocol result.
- [x] Run a read-only canary against an explicitly selected repository without advancing watermarks or changing GitHub state.
- [ ] Perform the explicit forced deployment only after accepting ownership of colliding public skill names.
- [ ] Verify the installed bundle, adapters, structured records, dashboard, flags, insights, and independent repository watermarks.
- [ ] Retire the legacy skills. The legacy data migration is complete, and its tools and the compatibility parser have been removed.
