---
name: Bug
about: A skill, the deployer, or a repository tool does something it should not
title: ''
labels: bug
assignees: ''
---

<!-- An action-oriented title naming the component, such as "deploy.py ..." or "review-prs ...".
     Remove personal paths, organization names, credentials, and private repository contents from everything
     below. Before 1.0.0 a suspected vulnerability is filed here too, as .github/SECURITY.md says; from 1.0.0 it
     goes through private vulnerability reporting, not an issue. -->

## Reproduction

<!-- The smallest sequence that shows the problem: the exact command or skill invocation, with its arguments, and
     any configuration it depends on. If possible, identify the component involved. -->

## Expected

## Actual

<!-- What happens instead, with the relevant output or error, trimmed to the part that shows the problem. -->

<!-- Name any known siblings: the same defect elsewhere, such as another caller of the same code or its twin in the
     other shell. -->

## Introduced

<!-- When the defect was written, not when it was found: the commit and pull request that wrote the defective code
     or text, found with git log -S or -G, or git blame, on the lines the issue cites; the first release tag that
     holds that commit (git tag --contains); and whether that pull request was itself a fix, by its title's type.
     For drift against an outside page, name the upstream change that made the line stale instead, such as a
     runtime version or changelog entry. When the line is older than the history you reached, write predates <tag>;
     when you cannot tell, write unknown. Either way, name what you searched. -->

## Commit and environment

<!-- The output of python deploy.py check, run in the clone the skills were deployed from: it names the deployed
     commit and the versions of Python, Git, PowerShell, ShellCheck, gh, Claude Code, Codex, and Copilot CLI.
     Below it, name the runtime the problem showed in and the model the skill ran on, since several skills behave
     differently by model. -->

Runtime and model:
