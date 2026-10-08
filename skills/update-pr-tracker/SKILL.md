---
name: update-pr-tracker
description: "Update the owned dashboard section for pull requests the configured user authors, reviews, or participates in. Use it when asked to refresh the pull request tracker or see which pull requests need attention."
argument-hint: "[owner/repo ... | --repository-set NAME] [--no-review] [--remove owner/repo#number ...]"
allowed-tools: ["Bash(python -B \"${CLAUDE_SKILL_DIR}/scripts/*)", "PowerShell(python -B \"${CLAUDE_SKILL_DIR}/scripts/*)", "AskUserQuestion", "Skill"]
---

# Update PR tracker

Every step below is one command of the tracker pipeline script, run exactly as shown; do not call `gh`, write the tracker input yourself, or read the scripts to work out what to do. Commands print one fact per line and exit 0 on success; a last line `FAILED <reason>` is an expected failure to report, not a reason to improvise. Never act on GitHub: this skill does not approve, comment on, or otherwise review a pull request.

1. **Collect** open pull requests, passing the user's `--repository owner/repo` (repeatable) or `--repository-set NAME`, or neither for the configured `update-pr-tracker` set:
   ```bash
   python -B "${CLAUDE_SKILL_DIR}/scripts/tracker_pipeline.py" collect
   ```
   It prints `REPOSITORY <owner/repo> pulls=<count>` per repository and `INPUT <input file>`, the file it wrote under a new temporary directory. Use the same scope on every run, or rows for the other repositories disappear. If any repository prints `REPOSITORY_FAILED <owner/repo> <error>`, no input is written and the command ends with a `FAILED` line: report each error and stop, so the dashboard keeps its previous rows rather than losing that repository's.
2. **Update** the dashboard from the `INPUT` file, adding `--remove "<owner/repo#number>"` for each removal and `--candidates` unless `--no-review` was given:
   ```bash
   python -B "${CLAUDE_SKILL_DIR}/scripts/tracker_pipeline.py" update --input "<input file>" --candidates
   ```
   It prints `UPDATED <dashboard> rows=<count>`, `GITHUB_CALLS <count>`, and, with `--candidates`, `CANDIDATE missing|stale <owner/repo#number>` for each relevant pull request whose AI review is missing or stale. Never edit the dashboard by hand.
3. **Offer reviews** unless `--no-review` was given. If there are `CANDIDATE` lines, list each `owner/repo#number` with its status and ask whether to generate or refresh those reviews, without token, usage, or cost estimates. When any candidate is `stale`, ask in the same AskUserQuestion call how to re-review the stale ones: `auto` (suggested; it decides per pull request from how much changed since its last review), `full` (the whole pull request again), or `incremental` (only the files that changed since the last review). Ask once for the run, never per pull request. Without confirmation, stop. On confirmation, invoke `review-prs` once for every confirmed pull request, with `--pull <owner/repo#number>` for each `missing` and `--re-review <owner/repo#number>` for each `stale`, plus `--scope <scope>` with the chosen scope when any is `stale`, so they are all reviewed in one pass.
4. **Refresh** after that review run finishes: run step 1 again, then step 2 with the same `--remove` arguments and without `--candidates`. Report every pull request whose review or re-review failed, with its error; those rows keep their `missing` or `stale` state and are offered again next run.

If the user says a pull request "is approved," "looks good," or "can be removed," pass it with `--remove owner/repo#number`. That removes only its row for this run; it is the user's assessment, not authorization to act on GitHub.

If the user wants an author shown under a different name, that belongs in `dashboard.author_names` (login to display name), not in the collected input.
