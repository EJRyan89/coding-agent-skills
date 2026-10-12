---
name: implement-change
description: >-
  Procedure for making a change in a repository that publishes an implementation profile
  (docs/implementing-changes.md), from the worktree and the plan to the pull request. Use it when asked to implement
  an issue or make a change in such a repository.
allowed-tools: ["Bash(gh pr create *)", "PowerShell(gh pr create *)"]
---

# Implementing a change

A change is finished only when it is tested, documented, validated, and opened as a pull request. The steps below are in order; do not edit before step 1 is done.

Every repository-specific fact comes from the repository's implementation profile, `docs/implementing-changes.md`. Read it first, then its sections as each step names them: *Worktree*, *Size gate*, *Model guidance*, *Contract files*, *Validation*, *Documentation*, *Pull requests*. If the profile or a section is missing, stop and tell the user. Also follow the repository's own instruction file (`CLAUDE.md`, `AGENTS.md`), and any procedure that specializes this one for the kind of change.

## 1. Worktree

Create the task's working tree with the command in *Worktree*, then move the session into it with the runtime's worktree transition, before planning: plans and project memory are keyed by working directory.

## 2. Intake

Start from an issue the user hands over or a need they describe; an open issue alone is not the signal to start. Restate the scope and where it stops before exploring.

## 3. Bug class

For a defect, find its class before planning the fix: the same pattern elsewhere, the other callers of the same reader or writer, and its twin in another shell or runtime. Fix each sibling in the same change, or file a follow-up issue for it and name that issue in the pull request. Fixing only the named instance leaves the rest to be found again.

## 4. Size gate

Apply *Size gate*. A change that passes is implemented on the current model without a plan. Otherwise plan.

## 5. Plan

Plan in the runtime's read-only planning mode, on the strongest model it offers. Read the files the change touches and their tests. Settle real design choices with the user. The deliverable is a handoff prompt that stands alone, in a fenced block:

- the issue and the scope, with where it stops;
- the files to change;
- the tests to write first;
- the validation to run, from *Validation*;
- the documentation to update, from *Documentation*.

Choose the model by this rubric:

- **Strongest** when *Model guidance* names the area, a *Contract files* entry is touched, or a design choice remains.
- **Balanced** when the plan is concrete and follows an existing pattern.
- **Cheapest** only for a fully specified mechanical edit whose test already exists, and never for a turn that later starts subagents, because a skill's model carries into them.

## 6. Approval and handoff

The user approves the plan and the model together, then chooses how to continue. State the trade-off once: staying in this session and switching the model keeps the context; a new session started from the handoff prompt costs less and starts clean. A skill cannot switch the model or mode itself, so give the user the command for their runtime from the table below.

| Runtime | Planning mode | Switch model |
|---|---|---|
| Claude Code | `/plan`, or start with `--permission-mode plan` | `/model`, or start with `--model <name>` |
| Codex CLI | `/plan` | `/model`, or start with `--model <name>` |
| Copilot CLI | `/plan` | `/model`, or start with `--model <name>` |

## 7. Implement

Work test first: write the failing test, then make it pass. Update *Documentation* in the same change. Run the local checks *Validation* names for the files the change touches, in full only where it says the change's reach warrants it, and get them green without skipping, weakening, or suppressing a check; rerun them after anything that changes the tree. Continuous integration runs the full gate; the gates *Validation* names as calling a model are yours to run. Read the whole diff once more for personal paths, credentials, and generated artifacts, and leave no scratch file or background command behind.

## 8. Pull request

Bring the branch up to date with its base as the repository's instructions describe; continuous integration validates the result, so it needs no local rerun. Title the pull request as *Pull requests* says. Write the body from the repository's pull request template to a file, naming each issue the change resolves in its own `Closes #<number>` line, and open the pull request:

```bash
gh pr create --title '<title>' --body-file '<file>'
```

When a continuous integration leg fails, reproduce that leg as *Validation* says, fix it, and push. While the pull request is open, bring it current by merging its base, never by rebasing; the merge may be made on the remote, so pull the branch before the next push. Do not merge.

