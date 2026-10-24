---
name: update-pr-dashboard
description: "Update the owned dashboard section for pull requests the configured user authors, reviews, or participates in. Use it when asked to refresh the pull request dashboard or tracker, or to see which pull requests need attention."
argument-hint: "[owner/repo ... | --repository-set NAME] [--no-review] [--remove owner/repo#number ...]"
allowed-tools: ["Bash({{PYTHON}} -B \"${CLAUDE_SKILL_DIR}/scripts/dashboard_pipeline.py\" *)", "PowerShell({{PYTHON}} -B \"${CLAUDE_SKILL_DIR}/scripts/dashboard_pipeline.py\" *)", "AskUserQuestion", "Skill"]
---

# Update PR dashboard

Every step below is one command of the dashboard pipeline script, run exactly as shown; do not call `gh`, write the tracker input yourself, or read the scripts to work out what to do. Commands print one fact per line and exit 0 on success; a last line `FAILED <reason>` is an expected failure to report, not a reason to improvise. Never act on GitHub: this skill does not approve, comment on, or otherwise review a pull request.

1. **Collect** open pull requests, passing the user's `--repository owner/repo` (repeatable) or `--repository-set NAME`, or neither for the configured `update-pr-dashboard` set:
   ```bash
   {{PYTHON}} -B "${CLAUDE_SKILL_DIR}/scripts/dashboard_pipeline.py" collect
   ```
   It prints `REPOSITORY <owner/repo> pulls=<count>` per repository and `INPUT <input file>`, the file it wrote under a new temporary directory. Use the same scope on every run, or rows for the other repositories disappear. If any repository prints `REPOSITORY_FAILED <owner/repo> <error>`, no input is written and the command ends with a `FAILED` line: report each error and stop, so the dashboard keeps its previous rows rather than losing that repository's. For a set, it also prints `UNTRACKED_REPOSITORY <owner/repo>` for each repository outside the set where the user authors, is asked to review, or has reviewed an open pull request: report them, since their pull requests never reach the dashboard (adding or muting one is below). `UNTRACKED_IGNORED <count>` counts those the user muted, which it does not name; report the count. `UNTRACKED_CHECK_FAILED <reason>` means that search failed; report it and carry on.
2. **Update** the dashboard from the `INPUT` file, adding `--remove "<owner/repo#number>"` for each removal and `--candidates` unless `--no-review` was given:
   ```bash
   {{PYTHON}} -B "${CLAUDE_SKILL_DIR}/scripts/dashboard_pipeline.py" update --input "<input file>" --candidates
   ```
   It prints `UPDATED <dashboard> rows=<count>`, `GITHUB_CALLS <count>`, a `STALE_SINCE_LAST_UPDATE <owner/repo#number>` line for each review that went stale since the previous update, in this session or an earlier one, because new commits arrived after it (report them now, whether or not step 3 goes on), and, with `--candidates`, `CANDIDATE missing|stale <owner/repo#number>` for each relevant pull request whose AI review is missing or stale, except one pinned by a status override (below). If it instead prints `PULL_FAILED <owner/repo#number> <error>` lines before its `FAILED` line, a GitHub call failed and the dashboard was left as it was: report each error and stop; a pull request that fails on every run can be left out with `--remove`. A `FAILED` line naming a removal means no collected pull request matches it: check the number with the user. Never edit the dashboard by hand.
3. **Offer reviews** unless `--no-review` was given. If there are `CANDIDATE` lines, list each `owner/repo#number` with its status and ask with AskUserQuestion whether to generate or refresh those reviews, without token, usage, or cost estimates. Without confirmation, stop. Once the user confirms, name each confirmed pull request with its own `--confirm`:
   ```bash
   {{PYTHON}} -B "${CLAUDE_SKILL_DIR}/scripts/dashboard_pipeline.py" review-arguments --confirm "<owner/repo#number>"
   ```
   If it prints `SCOPE_NEEDED <owner/repo#number>` lines, those confirmed pull requests are `stale`: ask in a second AskUserQuestion call how to re-review them, `auto` (suggested; it decides per pull request from how much changed since its last review), `full` (the whole pull request again), or `incremental` (only the files that changed since the last review or that the last review left unreviewed), then run the command again adding `--scope <scope>`. Ask once for the run, never per pull request. Then, through the `Skill` tool, invoke `review-prs` once with exactly the arguments the `REVIEW_ARGUMENTS <arguments>` line prints, so every confirmed pull request is reviewed in one pass. A `FAILED` line naming a pull request means the last update did not offer it for review: check it with the user.
4. **Refresh** after that review run finishes: run step 1 again, then step 2 with the same `--remove` arguments and without `--candidates`. Report every pull request whose review or re-review failed, with its error, and each pull request a `STALE_SINCE_LAST_UPDATE` line names, as a review that went stale since step 2's update, during the review run, because new commits arrived; those rows keep their `missing` or `stale` state and are offered again next run.

If the user says a pull request "is approved," "looks good," or "can be removed," pass it with `--remove owner/repo#number`. That removes only its row for this run; it is the user's assessment, not authorization to act on GitHub.

If the user wants an author shown under a different name, that belongs in `dashboard.author_names` (login to display name), not in the collected input.

If the user wants a pull request shown under a status of their own, such as `on hold` or `delegated`, that belongs in the configuration's `dashboard.status_overrides`: from the next update its row sits in a section named after the status, and the status may not be a computed state (`to review`, `awaiting response`, `my prs`, `my pull requests`, `drafts`, `missing`, `current`, `stale`, or `incomplete`, in any case). A pinned row still shows its AI review state, but it is never a `CANDIDATE` or a `STALE_SINCE_LAST_UPDATE` line, so step 3 never offers it for review; to have it reviewed, clear the override or run `review-prs` on it directly. Set it with this command, which validates the configuration before writing it and refuses a pull request the last update did not track; never edit the configuration or the dashboard by hand:
```bash
{{PYTHON}} -B "${CLAUDE_SKILL_DIR}/scripts/dashboard_pipeline.py" override --set "<owner/repo#number>=<status>"
```
It prints `SET` and `WROTE <configuration>`; a `FAILED` line naming the pull request means the tracker does not hold it, so check the number with the user, or run steps 1 and 2 first if the pull request is new. `--clear "<owner/repo#number>"` instead unpins one, printing `CLEARED`; nothing else clears an override, even once its pull request has closed. `override` alone prints `OVERRIDE <owner/repo#number> <status>` for each, or `OVERRIDE_UNTRACKED` for one whose pull request the tracker no longer holds, which the user may want to clear, then `OVERRIDES <count>`.

If the user wants a repository tracked, such as one an `UNTRACKED_REPOSITORY` line named, add it with this command, which validates the configuration before writing it; never edit the configuration by hand:
```bash
{{PYTHON}} -B "${CLAUDE_SKILL_DIR}/scripts/dashboard_pipeline.py" repository --add "<owner/repo>"
```
It adds the repository to the set step 1 reads without a scope, or to the user's `--repository-set NAME`. It prints `ADDED_REPOSITORY <owner/repo> reviewer=generic` when the repository had no entry and now has the generic reviewer, `ADDED_TO_SET <set> <owner/repo>`, and `WROTE <configuration>`. A `FAILED` line names a repository already in the set, an unknown set, or a configuration that does not validate; report it. Its pull requests appear from the next collection.

If the user wants an `UNTRACKED_REPOSITORY` line gone without tracking the repository, such as a sandbox or one they touch only in passing, mute it, or a pattern such as `"<owner>/*"` or `"<owner>/test-*"` (`*` any run of characters, `?` any one, matched in the owner and the name separately, ignoring case):
```bash
{{PYTHON}} -B "${CLAUDE_SKILL_DIR}/scripts/dashboard_pipeline.py" repository --ignore "<owner/repo>"
```
It records the entry in the configuration's `dashboard.ignored_repositories`, printing `IGNORED <entry>` and `WROTE <configuration>`; `--unignore "<entry>"`, naming the entry as it was muted, removes it, printing `UNIGNORED <entry>`. Muting changes only what step 1 names, never what it collects. A `FAILED` line names an entry already muted or not muted, one that is not an `owner/repo` identity or pattern, or one a repository in a set matches, since a tracked repository cannot be muted; report it.
