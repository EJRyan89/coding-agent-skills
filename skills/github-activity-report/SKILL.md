---
name: github-activity-report
description: "Report one user's GitHub contributions in one organization month by month: pull requests authored and merged, commits, pull requests reviewed, and reviews submitted. Use it when asked what someone contributed or reviewed over recent months."
argument-hint: "ORG USER [--months N]"
allowed-tools: ["Bash(python -B \"${CLAUDE_SKILL_DIR}/scripts/*)", "PowerShell(python -B \"${CLAUDE_SKILL_DIR}/scripts/*)"]
---

# GitHub activity report

Produce a calendar-month contribution table for `USER` in the GitHub organization `ORG`, covering the last `N` months (default 12, at most 36) and ending with the current, partial month. Both arguments are required; if either is missing, ask for it rather than guessing.

## Run

The report needs an authenticated GitHub CLI (`gh auth status`) whose token can read the organization's repositories, including SSO authorization when the organization enforces it.

```bash
python -B "${CLAUDE_SKILL_DIR}/scripts/github_activity_report.py" --org "<ORG>" --user "<USER>" --months "<N>"
```

Searches are spaced to stay under GitHub's search rate limits, so a 12-month report takes a few minutes; about three for a user with nearly 2,000 reviewed pull requests. That is longer than many runtimes' default shell-command timeout, so give the command a timeout of at least 10 minutes, or run it in the background and wait for it to exit before reading its output. Do not start a second run while one is still going. Progress goes to stderr; the report goes to stdout.

## Output

Show the script's stdout to the user as-is: a Markdown table with one row per month and a **Total** row, followed by notes. Do not recompute, round, or reinterpret the numbers.

- PRs authored and PRs merged come from GitHub search totals for pull requests created or merged in each month.
- Commits counts distinct default-branch commits whose author email is linked to the user's account.
- PRs reviewed counts distinct pull requests the user reviewed each month, and its total counts each pull request once. Reviews submitted counts every submitted review. Both exclude reviews on the user's own pull requests and pending reviews.

- Every count covers only the organization's repositories the current token can read. GitHub search leaves out other repositories without an error, so say this when presenting the numbers as someone's total activity.

If the notes contain a `WARNING` about the 1000-result Search API cap, pass it on as written. A PRs authored, PRs merged, or Commits warning names the undercounted days. A review warning names pull request update dates, not review dates, so it means both review columns may be low in any month and in their totals.

## Failures

The script fails closed and never prints a partial table. On failure it prints one line, `FAILED <reason> [<kind>]`, instead of the table and exits 1:

- `[prerequisite]` or `[authentication]`: install `gh` or run `gh auth login`.
- `[forbidden]`: the token cannot read some repositories or pull requests, often because it is not SSO-authorized for the organization.
- `[sso_partial]`: GitHub reported that it left out results from organizations the token is not SSO-authorized for. Authorize the token (`gh auth refresh`, or the SSO link on the token's settings page) and rerun.
- `[rate_limit]`: GitHub kept rate-limiting after retries, or asked for a long wait. Suggest rerunning later.
- `[incomplete]`: a search kept returning fewer results than GitHub reported, usually because results changed while it was paging or the search timed out. Suggest rerunning.
- Any other kind: report the message unchanged.
