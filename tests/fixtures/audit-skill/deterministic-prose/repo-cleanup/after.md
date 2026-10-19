---
name: repo-cleanup
description: "Clean up GitHub repos: switch to default branch, prune gone branches, remove stale worktrees, and fast-forward remaining branches. Sweeps all repos under {{REPOS_ROOT}} or targets a single repo by name/path."
argument-hint: "[<repo-name-or-path>]"
allowed-tools: ["Bash({{PYTHON}} -B \"${CLAUDE_SKILL_DIR}/scripts/repo_cleanup.py\" *)", "PowerShell({{PYTHON}} -B \"${CLAUDE_SKILL_DIR}/scripts/repo_cleanup.py\" *)", "AskUserQuestion"]
disable-model-invocation: true
---

Anything ambiguous is kept and reported, and only the user decides the remaining deletions.

Every step is one command of `repo_cleanup.py`, run exactly as shown. Do not run Git or `gh` yourself, parse Git output, track lists, or write glue code: the commands do all classification, safety checks, and bookkeeping, and print one tab-separated fact per line. Never work around a check they enforce.

## 1. Sweep

Pass the user's argument as the target, or omit it to sweep every repository:

```bash
{{PYTHON}} -B "${CLAUDE_SKILL_DIR}/scripts/repo_cleanup.py" sweep --repos-root "{{REPOS_ROOT}}" "<target>"
```

A single line `FAILED <reason>` (for example, the GitHub CLI is not signed in) means nothing ran and nothing was created: report it and stop. Otherwise it first prints `PLANS <directory>`, the new temporary directory that holds each repository's plan file, then one block per repository, starting with `REPO <path> <state>`, then `PLAN <plan file>` once a plan was applied, then only the lines that need you, and ends with `SWEPT <n>` and a count per state. Act on the blocks, not the exit status.

## 2. Each repository's state

- `cleaned`: show its `SUMMARY` lines, and ask the step 4 questions for its `CONFIRM_LOCAL`, `UNMERGED`, `CONFIRM_DETACHED`, and `ORPHAN` lines, using its `PLAN` file.
- `quiet`: nothing changed and nothing needs the user. List these repositories together on one line instead of showing a summary.
- `dirty`: a `DIRTY_MAIN <n>` line; handle it in step 3.
- `fetch-failed`: relay its `SUMMARY` lines.
- `error` or `git-failed`: report its `ERROR` line; nothing more runs for it. With a `PLAN` line, it failed partway through cleaning: first show its `SUMMARY` lines, which say what was done before the failure, and ask the step 4 questions for its `CONFIRM_LOCAL`, `UNMERGED`, `CONFIRM_DETACHED`, and `ORPHAN` lines.

A `CHECKOUT failed <reason>` line means the switch to the default branch failed; report it with that repository's summary. A repository with no `PLAN` line may show a `CHECKOUT switched` or `FF_DEFAULT` line, what was already done to its default branch; report it with the repository.

## 3. Dirty main worktrees

For each `dirty` repository, call `AskUserQuestion` with question `"<REPO_NAME>: The main worktree has uncommitted changes. Continue (branch switch will be skipped) or abort this repo?"`, header `"Dirty tree"`, and options `"Continue"` and `"Abort"`. For Abort, report the repository as aborted. For Continue, sweep that repository again without switching its branch, passing its path as the target and the first sweep's `PLANS` directory as the record it adds to, so that directory keeps every repository's plan:

```bash
{{PYTHON}} -B "${CLAUDE_SKILL_DIR}/scripts/repo_cleanup.py" sweep --repos-root "{{REPOS_ROOT}}" --record "<plans directory>" --skip-checkout "<repo>"
```

It prints the same block as the first sweep, now with the repository's state after cleaning; handle that state as step 2 describes.

## 4. Confirmations

Ask only these questions, and only when the sweep or a later command printed the matching lines.

**Local-only and closed branches.** For `CONFIRM_LOCAL <branch>` lines, call `AskUserQuestion` with question `"<REPO_NAME>: These branches have no remote tracking branch, or their pull request closed without merging. Delete or keep each?"` listing the branches, header `"Local branches"`, and options `"Delete all listed"`, `"Keep all"`, and `"Let me choose individually"`; for the last, ask per branch with `"Delete"` and `"Keep"`. A line `CONFIRM_LOCAL <branch> <worktree>` is a branch checked out in that clean worktree: list it as `<branch> (removes worktree <worktree>)`, since deleting it removes the worktree first. Then delete the chosen branches:

```bash
{{PYTHON}} -B "${CLAUDE_SKILL_DIR}/scripts/repo_cleanup.py" delete-local --plan "<plan file>" --branch "<branch>" --branch "<branch>"
```

**Unmerged branches.** For `UNMERGED <branch> <sha>` lines from the sweep or `delete-local`, call `AskUserQuestion` with question `"<REPO_NAME>: These branches are not fully merged into <DEFAULT_BRANCH>. Force-delete or keep?"` and header `"Unmerged"`. For up to four branches offer `"Force delete"` and `"Keep"` per branch; for more, offer `"Force delete all"`, `"Keep all"`, and `"Choose individually"`. Then force-delete only the confirmed branches:

```bash
{{PYTHON}} -B "${CLAUDE_SKILL_DIR}/scripts/repo_cleanup.py" force-delete --plan "<plan file>" --branch "<branch>"
```

Both print `DELETED`, `UNMERGED`, or `PRESERVED <branch> <reason>` (for a deletion Git refused, or a worktree its ignored files or the state it holds kept, its reason, and the command exits 1), `REMOVED` for a worktree removed with its branch, and `PRUNED_DIR <dir>` for each empty parent directory its removal left and the command removed, or a last line `FAILED <reason>` when the plan cannot be used or Git could not run; `SUMMARY` lines before it say what was done.

**Detached worktrees and leftover directories.** For `CONFIRM_DETACHED <path> <sha>` lines, call `AskUserQuestion` with question `"<REPO_NAME>: These worktrees have no branch checked out and no changes, and their commit is on <DEFAULT_BRANCH> or a remote branch. Remove or keep each?"` and header `"Detached"`. For `ORPHAN <path>` lines, call it with question `"<REPO_NAME>: These directories in the worktree area are no longer Git worktrees, and Git tracks none of their files. They cannot be recovered once deleted. Delete or keep each?"` and header `"Leftovers"`. Offer each list's options as for local branches. Then remove the chosen paths of both:

```bash
{{PYTHON}} -B "${CLAUDE_SKILL_DIR}/scripts/repo_cleanup.py" remove-directory --plan "<plan file>" --path "<path>" --path "<path>"
```

It prints `REMOVED` for a worktree, `DELETED_DIR` for a directory, `PRUNED_DIR <dir>` for each empty parent directory either left and the command removed, or `PRESERVED <path> <reason>` for one that stays (when Git refused, the worktree's ignored files or the state it holds kept it, deleting failed, the directory resolves outside the worktree areas, or Git tracks files in it, the command exits 1), or a last line `FAILED <reason>`; `SUMMARY` lines before it say what was done.

## 5. Summary

Once every question is answered, print the run's report from the first sweep's `PLANS` directory, with one `--aborted` for each repository the user aborted in step 3, by its `REPO` path, or none:

```bash
{{PYTHON}} -B "${CLAUDE_SKILL_DIR}/scripts/repo_cleanup.py" report --record "<plans directory>" --aborted "<repo>"
```

Show its lines' text as given: each `REPO <path> <state>` with the `SUMMARY`, `ERROR`, `CHECKOUT`, and `FF_DEFAULT` lines after it, the `QUIET` repositories on one line, and the last line, `PROCESSED`. A line `FAILED <reason>` instead means no report was given: when it names a dirty repository still to handle, handle it as step 3 says and run the report again; otherwise relay it.
