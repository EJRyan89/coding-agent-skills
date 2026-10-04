---
name: change-skill
description: >-
  Procedure for adding a skill to this repository or changing one: its SKILL.md, scripts, deploy metadata, or
  reference documentation. Use it when asked to add, create, change, fix, or extend a skill here. It covers the
  worktree, planning with the user, scaffolding, test-first implementation, documentation, full validation, and
  the pull request. Not for deployer-only, repository-tool, or documentation-only changes.
---

# Adding or changing a skill

A skill change is finished only when it is tested, documented, validated, and audited in one pull request. The steps below are in order; do not start editing before step 4.

`CLAUDE.md` holds the rules this procedure applies, and [`docs/adding-a-skill.md`](../../../docs/adding-a-skill.md) holds the skill contract. Read the sections each step names rather than working from memory.

## 1. Take the work into a worktree

From the hub, before planning, run `python tools/worktrees.py new <kind> <name>`, with `feat`, `fix`, or `docs` as the kind. Then move the session into the printed path with the runtime's worktree transition (in Claude Code, `EnterWorktree` with `path:`, never `name:`). Do this before planning: the plan and project memory are keyed by working directory. If the session is already in a read-only planning mode, plan in the hub and make the worktree the first act of step 4.

## 2. Intake

Start from a GitHub issue the user gives you (`gh issue view <number>`) or a need they describe. An open issue is not the signal to start; the user handing it to you is. Restate what the change is and where it stops before exploring.

## 3. Plan, and get approval

For a change, read the skill's `SKILL.md`, its `deploy-meta/<name>.json`, its scripts and their tests, and its section in [`docs/skills.md`](../../../docs/skills.md). Then settle, with the user where it is a real choice:

- **Arguments**: the `argument-hint`, in the notation `docs/skills.md` explains, and what the skill does without them.
- **Who starts it**: `disable-model-invocation: true` for a skill that deletes, deploys, or otherwise acts on the user's behalf; `user-invocable: false` too for a hidden dependency. Never set either on a skill another skill invokes by name ("Files" in `docs/adding-a-skill.md`).
- **What it runs**: only standard commands or tools from `deployer/tools.py`, declared in `tools` ("Commands skills may run").
- **What it needs**: `required_vars` for every `{{TOKEN}}` it uses, and nothing more ("Template values").
- **Scripts versus prose**: anything deterministic belongs in a tested script under `scripts/`, with prose kept for judgment and confirmations. A fence in Markdown is at most five lines, and names the skill's scripts through the skill-directory variable ("Paths to a skill's own files").
- **Description**: what the skill does and when to use it. Claude Code loads every model-invocable description into every session, so keep it short.

End the plan with a one-line implementation-model recommendation: a balanced model when the plan is concrete and follows an existing pattern, the strongest model when design judgment remains or the change touches rendering, quoting, trust boundaries, or the review pipeline. The user approves the plan and the model together.

## 4. Scaffold a new skill

For a new skill only, with the values the plan settled:

```bash
python tools/new_skill.py <name> --description "<description>" --argument-hint "<hint>" --tool gh
```

Add `--user-only` and `--opt-in` as planned, and omit `--argument-hint` or `--tool` when there is none. It writes the frontmatter, the metadata, and the generated part of the reference section, refuses without changing anything when the name is taken or a tool is unknown, and prints a `REMAINING` line for each thing validation still needs from you. A skill inside a bundle also needs its entry in `source.json`.

## 5. Implement test first

Write the failing test, then make it pass:

- A script's regression suite lives beside it under `scripts/`, named `test_*` or one of the other patterns in "Validation".
- A `{{TOKEN}}` in Bash or PowerShell needs a rendered execution fixture in `tests/deployer/` whose values contain spaces and other allowed punctuation.
- A contract between skills, such as one skill invoking another, belongs in `tests/ai-config/test_cross_skill_contracts.py`.

Keep `SKILL.md` to orchestration: the commands to run, what their output means, and what to ask the user.

## 6. Document in the same change

- After any frontmatter or metadata change, run `python tools/skill_reference.py --write`, then update the hand-written part of the skill's section in `docs/skills.md` to match what shipped: what each argument means, what it does without them, and an example.
- A new skill, or bundle, gets a row in the "Included skills" table in `README.md`, linking its section in `docs/skills.md`.
- A code-review skill's configuration or records belong in `docs/code-review-operations.md`.

`python tools/skill_reference.py` lists anything still missing.

## 7. Verify

Run `python -B tests/run_validation.py` in full and get it green. Never skip, weaken, or suppress a check to get there. Then read the whole diff once more for personal paths, organization names, and generated artifacts.

Validation never starts a runtime. When the change touches how a skill names its own files, its `allowed-tools`, its runtime adapter, or the agents it declares, also run the `runtime-canary` repository skill for the changed skills and record its result in the pull request.

## 8. Audit the cost

Validation checks the contract; `analyze-skill-cost` judges what the change costs: token footprint, deterministic work left to prose, delegation, and the `model`, `allowed-tools`, and listing frontmatter. From the worktree, run `analyze-skill-cost <name>` for each changed skill under `skills/`, and check that its `Scope` names `skills/<name>` here: `locate` reports `SCOPE source` when it audits this tree, never the deployed copy.

It is the deployed user-level skill. If it is not installed, or reports `SCOPE user` because the deployed copy predates the source scope, deploy from the hub on an up-to-date `main` and rerun it, or skip the audit and say why in the pull request. Resolve every MUST FIX before opening the pull request and rerun validation if that changed anything. A SUGGESTION may stay, but the pull request body names each one left and why.

## 9. Open the pull request

First bring the branch up to date: `git fetch origin main`, then `git rebase origin/main`, and rerun the full suite if that brought in commits. Then write the body from `.github/pull_request_template.md` to a file and run `gh pr create --body-file <file>`, with `Closes #<number>` when the work came from an issue. Later commits to the open pull request merge `origin/main` in first instead of rebasing, as "Issues and pull requests" in `CLAUDE.md` describes. After it merges, the worktree is retired from a session outside it: `repo-cleanup` on the hub, or by hand as "Retiring a worktree" in `docs/parallel-sessions.md` describes.

## Done when

- [ ] Validation passes in full, with regression coverage for every behavior change.
- [ ] For a path, `allowed-tools`, adapter, or agent change, the `runtime-canary` result is in the pull request.
- [ ] `docs/skills.md` and the README describe the skill as it shipped.
- [ ] `analyze-skill-cost` audited each changed skill from the source tree, with no MUST FIX left, or the pull request says why it did not run.
- [ ] The pull request is open from the template, naming its issue, and current with `main`.
- [ ] Nothing is left behind: no scratch files in the worktree and no background command still running.
