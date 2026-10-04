---
name: repo-cleanup
description: "Clean up GitHub repos: switch to default branch, prune gone branches, remove stale worktrees, and fast-forward remaining branches. Sweeps all repos under {{REPOS_ROOT}} or targets a single repo by name/path."
argument-hint: "[<repo-name-or-path>]"
allowed-tools: ["Bash(python -B \"${CLAUDE_SKILL_DIR}/scripts/*)", "PowerShell(python -B \"${CLAUDE_SKILL_DIR}/scripts/*)", "AskUserQuestion"]
disable-model-invocation: true
---

Automate repository housekeeping: put each repository on its default branch with the latest fetch, remove branches whose remote is gone and whose pull request proves them finished (including squash merges), clean up stale worktrees (preserving release branches and release-path worktrees), and fast-forward remaining branches. Anything ambiguous is kept and reported, and only the user decides the remaining deletions.

Every step is one command of `repo_cleanup.py`, run exactly as shown. Do not run Git or `gh` yourself, parse Git output, track lists, or write glue code: the commands do all classification, safety checks, and bookkeeping, and print one tab-separated fact per line.

## 1. Sweep

Choose a new plan directory outside every repository, such as `<temp directory>/repo-cleanup`. Pass the user's argument as the target, or omit it to sweep every repository:

```bash
python -B "${CLAUDE_SKILL_DIR}/scripts/repo_cleanup.py" sweep --repos-root "{{REPOS_ROOT}}" --plans "<plan directory>" "<target>"
```

A target is a repository name under `{{REPOS_ROOT}}` or an absolute path to a directory that contains a `.git` directory. An `ERROR` before any `REPO` line (for example, the GitHub CLI is not signed in) stops the run. Otherwise the sweep cleans every repository at once, each exactly as `sync`, `plan`, and `apply` would: it switches to the default branch, runs `git fetch --all --prune`, prunes worktree records, fast-forwards the default branch, classifies branches and worktrees, and performs only the safe actions, re-checking every recorded branch tip first.

It prints one block per repository, starting with `REPO <path> <state>`, then `PLAN <plan file>` once a plan was applied, then only the lines that need you, and ends with `SWEPT <n>` and a count per state. Exit status 2 means a helper failed in at least one repository (`helper-failed`): every block was still printed, so handle them, then report the failure and stop.

## 2. Each repository's state

- `cleaned`: show its `SUMMARY` lines, and ask the step 4 questions for its `CONFIRM_LOCAL` and `UNMERGED` lines, using its `PLAN` file.
- `quiet`: nothing changed and nothing needs the user. List these repositories together on one line instead of showing a summary.
- `dirty`: `DIRTY_MAIN <n>` means nothing was switched or fetched; handle it in step 3.
- `fetch-failed`: no plan was made and nothing was deleted, removed, or fast-forwarded. Relay its `SUMMARY` lines.
- `error` or `helper-failed`: report its `ERROR` line; nothing more runs for it.

A `CHECKOUT failed <reason>` line means the switch to the default branch failed; report it with that repository's summary.

## 3. Dirty main worktrees

For each `dirty` repository, call `AskUserQuestion` with question `"<REPO_NAME>: The main worktree has uncommitted changes. Continue (branch switch will be skipped) or abort this repo?"`, header `"Dirty tree"`, and options `"Continue"` and `"Abort"`. For Abort, report the repository as aborted. For Continue, sweep that repository again without switching its branch, passing its path as the target:

```bash
python -B "${CLAUDE_SKILL_DIR}/scripts/repo_cleanup.py" sweep --repos-root "{{REPOS_ROOT}}" --plans "<plan directory>" --skip-checkout "<repo>"
```

It prints the same block as the first sweep, now with the repository's state after cleaning; handle that state as step 2 describes.

## 4. Confirmations

Ask only these questions, and only when the sweep, `apply`, or a later command printed the matching lines. Use the repository's plan file in every command.

**Local-only branches.** For `CONFIRM_LOCAL <branch>` lines, call `AskUserQuestion` with question `"<REPO_NAME>: The following local-only branches have no remote tracking. Delete or keep each?"` listing the branches, header `"Local branches"`, and options `"Delete all listed"`, `"Keep all"`, and `"Let me choose individually"`; for the last, ask per branch with `"Delete"` and `"Keep"`. Then delete the chosen branches:

```bash
python -B "${CLAUDE_SKILL_DIR}/scripts/repo_cleanup.py" delete-local --plan "<plan file>" --branch "<branch>" --branch "<branch>"
```

**Unmerged branches.** For `UNMERGED <branch> <sha>` lines from the sweep, `apply`, or `delete-local`, call `AskUserQuestion` with question `"<REPO_NAME>: These branches are not fully merged into <DEFAULT_BRANCH>. Force-delete or keep?"` and header `"Unmerged"`. For up to four branches offer `"Force delete"` and `"Keep"` per branch; for more, offer `"Force delete all"`, `"Keep all"`, and `"Choose individually"`. Then force-delete only the confirmed branches:

```bash
python -B "${CLAUDE_SKILL_DIR}/scripts/repo_cleanup.py" force-delete --plan "<plan file>" --branch "<branch>"
```

Both commands act only on branches the plan offered or reported, re-check the recorded tip, and print `DELETED`, `UNMERGED`, or `PRESERVED <branch> <reason>`.

## 5. Summary

The sweep (for every repository that is not `quiet`), `apply`, `delete-local`, and `force-delete` each give the repository's current summary as `SUMMARY <text>` lines. Show the text of the last summary printed for each repository exactly as given; `summary --plan "<plan file>"` prints it again. It covers the default branch, deleted branches, removed worktrees, fast-forwarded and diverged branches, skipped dirty worktrees, branches kept because their pull request status was unverified or unmatched, protected release worktrees, and any unmerged, local-only, preserved, or gone-with-open-PR branches and removed empty directories.

Finish with `Processed <N> repositories.` from `SWEPT`, followed by how many were skipped because their fetch failed, the user aborted them, or they reported an `ERROR`.

## Safety guarantees

These are enforced by the commands; never work around them by running Git yourself.

- `release/*` branches are never deleted. A worktree is never removed when its branch matches `release/*` or its path contains a `/release/` segment.
- A branch is deleted automatically only when its remote branch is gone and its pull request status is `MERGED`, `CLOSED`, or `NONE`, and only with `git branch -d`, except that a squash- or rebase-merged branch that `-d` refuses is deleted with `git branch -D` when its status is `MERGED` and its tip has not moved. A merged or closed pull request counts only when its head is in the same repository, there are no newer upstream commits, and it ended at the branch's exact local tip. A merged pull request also counts when the local tip is one of its commits and every later commit is a merge whose non-first parents are reachable from the fetched default branch, as GitHub's "Update branch" makes; a later commit with any other changes leaves the branch `UNMATCHED`. Branches whose status is `OPEN`, `UNMATCHED`, or `UNKNOWN` (any failed or possibly truncated `gh` query) are kept and reported, never deleted or offered for deletion.
- Otherwise `git branch -D` runs only through `force-delete`, after the user confirmed, and only for a branch reported `UNMERGED` whose tip has not moved. A `CLOSED` or `NONE` branch that is not fully merged is always reported `UNMERGED`, never forced.
- Worktrees with uncommitted or untracked changes are never removed, only reported. Worktrees are removed only by `git worktree remove` through `remove_worktree.sh`; when Git refuses, the worktree and its branch are preserved and reported.
- Empty directories are removed only with `rmdir`, only for parents of worktrees removed in this run, and never at or above `{{REPOS_ROOT}}/Worktrees` or the repository root.
- Fast-forwards are fast-forward only. Diverged branches are reported, never rebased, merged, or reset. The main worktree's files are never stashed, reset, or cleaned.
- When a repository's fetch fails, nothing after the fetch runs for it. Earlier steps may already have switched its checkout, and a multi-remote fetch may have updated some remote-tracking refs before failing.
