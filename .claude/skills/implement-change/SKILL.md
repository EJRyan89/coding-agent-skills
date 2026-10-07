---
name: implement-change
description: >-
  Procedure for making a change in a repository that publishes an implementation profile
  (docs/implementing-changes.md), from the worktree and the plan to the pull request. Use it when asked to implement
  an issue or make a change in such a repository.
---

# Implementing a change

A change is finished only when it is tested, documented, validated, and opened as a pull request. The steps below are in order; do not edit before step 1 is done.

Every repository-specific fact comes from the repository's implementation profile, `docs/implementing-changes.md`. Read it first, then its sections as each step names them: *Worktree*, *Size gate*, *Model guidance*, *Contract files*, *Validation*, *Documentation*. If the profile or a section is missing, stop and tell the user. Also follow the repository's own instruction file (`CLAUDE.md`, `AGENTS.md`), and any procedure that specializes this one for the kind of change.

## 1. Worktree

Create the task's working tree with the command in *Worktree*, then move the session into it with the runtime's worktree transition, before planning: plans and project memory are keyed by working directory.

## 2. Intake

Start from an issue the user hands over or a need they describe; an open issue alone is not the signal to start. Restate the scope and where it stops before exploring.

## 3. Size gate

Apply *Size gate*. A change that passes is implemented on the current model without a plan, and the pull request says so with the reason. Otherwise plan.

## 4. Plan

Plan in the runtime's read-only planning mode, on the strongest model it offers. Read the files the change touches and their tests. Settle real design choices with the user. The deliverable is a handoff prompt that stands alone, in a fenced block:

- the issue and the scope, with where it stops;
- the files to change;
- the tests to write first;
- the validation to run, from *Validation*;
- the documentation to update, from *Documentation*;
- a one-line model recommendation with its reason.

Choose the model by this rubric:

- **Strongest** when *Model guidance* names the area, a *Contract files* entry is touched, or a design choice remains.
- **Balanced** when the plan is concrete and follows an existing pattern.
- **Cheapest** only for a fully specified mechanical edit whose test already exists, and never for a turn that later starts subagents, because a skill's model carries into them.

## 5. Approval and handoff

The user approves the plan and the model together, then chooses how to continue. State the trade-off once: staying in this session and switching the model keeps the context; a new session started from the handoff prompt costs less and starts clean. A skill cannot switch the model or mode itself, so give the user the command for their runtime from the table below.

| Runtime | Planning mode | Switch model |
|---|---|---|
| Claude Code | `/plan`, or start with `--permission-mode plan` | `/model`, or start with `--model <name>` |
| Codex CLI | `/plan` | `/model`, or start with `--model <name>` |
| Copilot CLI | `/plan` | `/model`, or start with `--model <name>` |

## 6. Implement

Work test first: write the failing test, then make it pass. Update *Documentation* in the same change. Run *Validation* in full and get it green without skipping, weakening, or suppressing a check; rerun it after anything that changes the tree. Read the whole diff once more for personal paths, credentials, and generated artifacts, and leave no scratch file or background command behind.

## 7. Pull request

Bring the branch up to date with its base as the repository's instructions describe, and rerun validation if that brought in commits. Write the body from the repository's pull request template to a file and open the pull request with `gh pr create --body-file <file>`, naming `Closes #<number>` for an issue. In its two model lines, name the model that planned (or "no plan" with the size-gate reason) and the model that implemented. Do not merge.

