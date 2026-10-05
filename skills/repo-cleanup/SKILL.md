---
name: repo-cleanup
description: "Clean up GitHub repos: switch to default branch, prune gone branches, remove stale worktrees, and fast-forward remaining branches. Sweeps all repos under {{REPOS_ROOT}} or targets a single repo by name/path."
argument-hint: "[<repo-name-or-path>]"
allowed-tools: ["Bash(python -B \"${CLAUDE_SKILL_DIR}/scripts/*)", "PowerShell(python -B \"${CLAUDE_SKILL_DIR}/scripts/*)", "AskUserQuestion"]
disable-model-invocation: true
---

Anything ambiguous is kept and reported, and only the user decides the remaining deletions.

Every step is one command of `repo_cleanup.py`, run exactly as shown. Do not run Git or `gh` yourself, parse Git output, track lists, or write glue code: the commands do all classification, safety checks, and bookkeeping, and print one tab-separated fact per line. Never work around a check they enforce.

## 1. Sweep

Pass the user's argument as the target, or omit it to sweep every repository:

```bash
python -B "${CLAUDE_SKILL_DIR}/scripts/repo_cleanup.py" sweep --repos-root "{{REPOS_ROOT}}" "<target>"
```

It first prints `PLANS <directory>`, the new temporary directory that holds each repository's plan file.

An `ERROR` before any `REPO` line (for example, the GitHub CLI is not signed in) stops the run. Otherwise it prints one block per repository, starting with `REPO <path> <state>`, then `PLAN <plan file>` once a plan was applied, then only the lines that need you, and ends with `SWEPT <n>` and a count per state. Exit status 2 means a helper failed in at least one repository (`helper-failed`): every block was still printed, so handle them, then report the failure and stop.

## 2. Each repository's state

- `cleaned`: show its `SUMMARY` lines, and ask the step 4 questions for its `CONFIRM_LOCAL` and `UNMERGED` lines, using its `PLAN` file.
- `quiet`: nothing changed and nothing needs the user. List these repositories together on one line instead of showing a summary.
- `dirty`: a `DIRTY_MAIN <n>` line; handle it in step 3.
- `fetch-failed`: relay its `SUMMARY` lines.
- `error` or `helper-failed`: report its `ERROR` line; nothing more runs for it.

A `CHECKOUT failed <reason>` line means the switch to the default branch failed; report it with that repository's summary.

## 3. Dirty main worktrees

For each `dirty` repository, call `AskUserQuestion` with question `"<REPO_NAME>: The main worktree has uncommitted changes. Continue (branch switch will be skipped) or abort this repo?"`, header `"Dirty tree"`, and options `"Continue"` and `"Abort"`. For Abort, report the repository as aborted. For Continue, sweep that repository again without switching its branch, passing its path as the target:

```bash
python -B "${CLAUDE_SKILL_DIR}/scripts/repo_cleanup.py" sweep --repos-root "{{REPOS_ROOT}}" --skip-checkout "<repo>"
```

It prints the same block as the first sweep, now with the repository's state after cleaning; handle that state as step 2 describes.

## 4. Confirmations

Ask only these questions, and only when the sweep or a later command printed the matching lines.

**Local-only branches.** For `CONFIRM_LOCAL <branch>` lines, call `AskUserQuestion` with question `"<REPO_NAME>: The following local-only branches have no remote tracking. Delete or keep each?"` listing the branches, header `"Local branches"`, and options `"Delete all listed"`, `"Keep all"`, and `"Let me choose individually"`; for the last, ask per branch with `"Delete"` and `"Keep"`. Then delete the chosen branches:

```bash
python -B "${CLAUDE_SKILL_DIR}/scripts/repo_cleanup.py" delete-local --plan "<plan file>" --branch "<branch>" --branch "<branch>"
```

**Unmerged branches.** For `UNMERGED <branch> <sha>` lines from the sweep or `delete-local`, call `AskUserQuestion` with question `"<REPO_NAME>: These branches are not fully merged into <DEFAULT_BRANCH>. Force-delete or keep?"` and header `"Unmerged"`. For up to four branches offer `"Force delete"` and `"Keep"` per branch; for more, offer `"Force delete all"`, `"Keep all"`, and `"Choose individually"`. Then force-delete only the confirmed branches:

```bash
python -B "${CLAUDE_SKILL_DIR}/scripts/repo_cleanup.py" force-delete --plan "<plan file>" --branch "<branch>"
```

Both print `DELETED`, `UNMERGED`, or `PRESERVED <branch> <reason>`.

## 5. Summary

The sweep (for every repository that is not `quiet`), `delete-local`, and `force-delete` each give the repository's current summary as `SUMMARY <text>` lines. Show the text of the last summary printed for each repository exactly as given; `summary --plan "<plan file>"` prints it again.

Finish with `Processed <N> repositories.` from `SWEPT`, followed by how many were skipped because their fetch failed, the user aborted them, or they reported an `ERROR`.
