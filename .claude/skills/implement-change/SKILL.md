---
name: implement-change
description: >-
  Procedure for making a change in a repository that publishes an implementation profile
  (docs/implementing-changes.md), from the worktree and the plan to the pull request. Use it when asked to implement
  an issue or make a change in such a repository.
allowed-tools: ["Bash(python -B .claude/skills/implement-change/scripts/pull_request.py *)", "PowerShell(python -B .claude/skills/implement-change/scripts/pull_request.py *)"]
---

# Implementing a change

A change is finished only when it is tested, documented, validated, and opened as a pull request. The steps below are in order; do not edit before step 1 is done.

Every repository-specific fact comes from the repository's implementation profile, `docs/implementing-changes.md`. Read it first, then its sections as each step names them: *Worktree*, *Size gate*, *Model guidance*, *Contract files*, *Validation*, *Documentation*, *Pull requests*. If the profile or a section is missing, stop and tell the user. Also follow the repository's own instruction file (`CLAUDE.md`, `AGENTS.md`), and any procedure that specializes this one for the kind of change.

## 1. Worktree

Settle the change's title type from the request first; *Pull requests* lists the types. Then follow *Worktree* for the place the session started. From the main checkout, create the task's working tree with its command and move the session into it with the runtime's worktree transition. From a worktree the runtime already made for the session, stay in it, create no second tree, and start its branch from the current base as *Worktree* says. Do this before planning: plans and project memory are keyed by working directory. If intake or the bug class later changes the type, rename the branch as *Worktree* says before its first push.

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
- the documentation to update, from *Documentation*;
- the pull request's title, and whether the change owes an upgrade note, from *Contract files* and *Pull requests*.

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

`.claude/skills/implement-change/scripts/pull_request.py` does the deterministic part: it drafts the body from the repository's pull request template, checks the title, the body, and the branch, and opens the pull request with the title read from its file, so no title is pasted into a command. Any command of it that cannot finish prints a last line `FAILED <reason>`: report it and stop. Take these in order:

1. Draft the body, with one `--closes` for each issue the change resolves and none when it resolves none:

   ```bash
   python -B .claude/skills/implement-change/scripts/pull_request.py draft --closes '<number>'
   ```

   It prints `TEMPLATE "<file>"`, the template it read, a `CLOSES <number>` line for each issue, and the `BODY` and `TITLE` files. Write the title, as *Pull requests* says, on one line in the `TITLE` file it names, and fill the `BODY` file: replace each template comment and empty item with what it asks for, and state the upgrade note when the change owes one.
2. Bring the branch up to date with its base as *Pull requests* says; continuous integration validates the result, so it needs no local rerun.
3. Check both files and the branch against the base *Pull requests* names, such as `origin/main`, then run *Validation*'s title and body checks on the same files. It prints the `TITLE` it read, a `CLOSES` line for each issue the title or the body closes by any closing reference GitHub reads, and `BASE <base> <commit>`, the base's tip after its fetch, then a line for each problem: `TITLE_EMPTY` or `TITLE_LINES <count>` for a title file that holds no title or more than one line; `TITLE_CLOSES <title>` for a title holding a closing reference, which the squash commit carries, so name that issue in the body's `Closes #<number>` line instead; `PLACEHOLDER <line> <what>`, `CLOSES_LINE <line> <text>`, or `HEADING_MISSING <heading>` for a template comment or empty item left in the body, a line holding a closing reference, anywhere, inside a comment you wrote too, and in any form GitHub reads, that is not a `Closes #<number>` line of its own, or a template heading the body dropped; `BEHIND <base> <commit>` for a branch to bring up to date (step 2); `NO_COMMITS <base>` for a branch with nothing to open; and `UNCOMMITTED <count>` for changed or untracked files the pull request would leave out. Fix what each line names and rerun until it prints `READY`:

   ```bash
   python -B .claude/skills/implement-change/scripts/pull_request.py check --title-file "<title file>" --body-file "<body file>" --base "<base>"
   ```

4. Push the branch under its own name, as *Pull requests* says, so the pull request has a branch to open from.
5. Open the pull request. `open` repeats the check, refuses a branch whose upstream does not hold `HEAD` with `UNPUSHED <upstream>`, or `UNPUSHED no upstream`, so push it (step 4), and prints `PULL_REQUEST <url>`:

   ```bash
   python -B .claude/skills/implement-change/scripts/pull_request.py open --title-file "<title file>" --body-file "<body file>" --base "<base>"
   ```

When a continuous integration leg fails, reproduce that leg as *Validation* says, fix it, and push. While the pull request is open, bring it current by merging its base, never by rebasing; the merge may be made on the remote, so pull the branch before the next push. If a push is rejected, fetch and inspect the remote branch before anything else. Do not merge.

