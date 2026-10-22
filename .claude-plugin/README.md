# coding-agent-skills for Claude Code

These are the skills of [coding-agent-skills](https://github.com/EJRyan89/coding-agent-skills), packaged as Claude Code plugins. The same skills install through the repository's deployer, which also serves Codex and GitHub Copilot CLI. [Choosing a route](https://github.com/EJRyan89/coding-agent-skills/blob/main/docs/installation.md#choosing-a-route) compares the two.

The marketplace offers these plugins:

- `coding-agent-skills`: code review of pull requests and documents, skill and agent-configuration audits, memory curation, GitHub reports, and repository cleanup.
- `coding-agent-skills-dotnet`: C# formatting and dependency questions, which the deployer installs only when asked.

## Installing

In Claude Code:

```text
/plugin marketplace add EJRyan89/coding-agent-skills
/plugin install coding-agent-skills@ejryan89
```

From a shell, the same two steps are `claude plugin marketplace add EJRyan89/coding-agent-skills` and `claude plugin install coding-agent-skills@ejryan89 --config "repos_root=<folder>"`. Install `coding-agent-skills-dotnet@ejryan89` the same way.

`coding-agent-skills` asks for one option, `repos_root`: the absolute path of the folder holding your git repositories, which `repo-cleanup` sweeps. `/plugin install` asks for it, and `claude plugin configure coding-agent-skills@ejryan89` shows or changes it. The value is used as typed: unlike the deployer, the plugin checks it against no character allowlist, and `repo-cleanup` refuses only an empty or relative path.

Start a skill with its plugin's name before it, such as `/coding-agent-skills:review-prs`. The prefix keeps these skills apart from another suite's skills of the same name. [Skills](https://github.com/EJRyan89/coding-agent-skills/blob/main/docs/skills.md) describes each skill and its arguments.

## Requirements

- **Python:** `python` must start a Python as new as [Requirements](https://github.com/EJRyan89/coding-agent-skills/blob/main/docs/installation.md#requirements) names. A plugin is one tree for every platform, so its skills and its hook run `python` by that name. On Linux that means a package such as `python-is-python3`.
- **The tools a skill needs**, such as `gh`: they are listed under "Needs" in [Skills](https://github.com/EJRyan89/coding-agent-skills/blob/main/docs/skills.md#summary) and installed as [Installing the tools](https://github.com/EJRyan89/coding-agent-skills/blob/main/docs/installation.md#installing-the-tools) says.

## What a plugin install does not give

- **The reviewer guard runs in every session.** The code-review skills' reviewer agent is guarded by a hook. Claude Code ignores a plugin agent's own hooks, so the plugin runs the guard on every Read, Grep, Glob, Write, Edit, and Bash call of every session while it is enabled. The guard passes every call but the reviewer's. That is one Python start per call. If `python` is missing or broken, those calls are blocked until the plugin is disabled. An organization setting that admits only managed hooks turns the guard off.
- **The deployer's guarantees:** journaled rollback and recovery, an ownership manifest that refuses to replace a skill another source owns, a permanent backup of a skill you changed, and configured values, such as the repositories root, checked against an allowlist. Claude Code replaces the plugin's copy at each update, and a change made inside that copy is lost.
- **`update-coding-agent-skills`.** It updates a deployer's clone, so it comes only through the deployer.
- **Releases only.** The plugin follows releases. To follow `main`, use the deployer.

## Updating and removing

Update with `claude plugin update coding-agent-skills@ejryan89`, or from `/plugin`. Claude Code does not update a plugin from this marketplace by itself unless you turn on its auto-update. Remove it with `claude plugin uninstall coding-agent-skills@ejryan89`.
