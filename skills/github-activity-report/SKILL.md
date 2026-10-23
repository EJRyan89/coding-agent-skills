---
name: github-activity-report
description: "Report GitHub contributions in one organization month by month for a user, several users, or a team: pull requests authored and merged, commits, and reviews, optionally kept in a history file. Use it when asked what someone or a group contributed or reviewed over recent months."
argument-hint: "ORG USER ... | --team SLUG [--months N] [--history PATH] [--metric METRIC]"
allowed-tools: ["Bash({{PYTHON}} -B \"${CLAUDE_SKILL_DIR}/scripts/github_activity_report.py\" *)", "PowerShell({{PYTHON}} -B \"${CLAUDE_SKILL_DIR}/scripts/github_activity_report.py\" *)"]
---

# GitHub activity report

Produce a calendar-month contribution table in the GitHub organization `ORG`, covering the last `N` months (default 12, at most 36) and ending with the current, partial month, for one `USER`, several users, or the current members of the organization team `SLUG`. `ORG` and at least one user or a team are required; if either is missing, ask for it rather than guessing. Pass `--history` and `--metric` only when the user asks for them.

## Run

The report needs an authenticated GitHub CLI (`gh auth status`) whose token can read the organization's repositories, including SSO authorization when the organization enforces it, and, for a team, its members (the `read:org` scope on a classic token). For one user:

```bash
{{PYTHON}} -B "${CLAUDE_SKILL_DIR}/scripts/github_activity_report.py" --org "<ORG>" --user "<USER>" --months "<N>"
```

For a group, repeat `--user "<USER>"` for each login, or pass `--team "<SLUG>"` instead. Add `--history "<PATH>"` to keep every run's months in a CSV file at the path the user names, and `--metric "<METRIC>"` to choose what the comparison compares: `prs_authored`, `prs_merged` (the default), `commits`, `prs_reviewed`, or `reviews_submitted`. The history file holds per-person activity over time: tell the user to keep it private, and never suggest a path for it yourself.

Searches are spaced to stay under GitHub's search rate limits, so a 12-month report takes a few minutes per user; about three for a user with nearly 2,000 reviewed pull requests. With a history, a later run searches only the current month and the months the history lacks or captured while they were partial. That is longer than many runtimes' default shell-command timeout, so give the command a timeout of at least 10 minutes per user, or run it in the background and wait for it to exit before reading its output. Do not start a second run while one is still going. Progress goes to stderr; the report goes to stdout.

## Output

Show the script's stdout to the user as-is. For one user without a history it is a Markdown table with one row per month and a **Total** row; for a group, or with a history, it is one comparison table of the metric, with a row per month and a column per user. Notes follow that say what the numbers count. Do not recompute, round, or reinterpret the numbers, and pass on every note, including any `WARNING`, as written.

## Failures

The script fails closed and never prints a partial table or writes a partial history: a failure for any user leaves the history file as it was. On failure it prints one line, `FAILED <reason> [<kind>]`, instead of the table and exits 1; in a group the reason starts with the user it failed for:

- `[prerequisite]` or `[authentication]`: install `gh` or run `gh auth login`.
- `[forbidden]`: the token cannot read some repositories or pull requests, often because it is not SSO-authorized for the organization.
- `[sso_partial]`: GitHub reported that it left out results from organizations the token is not SSO-authorized for. Authorize the token (`gh auth refresh`, or the SSO link on the token's settings page) and rerun.
- `[scope]`: the token lacks `read:org`, which reading a team's members needs. Suggest `gh auth refresh -s read:org` and rerunning.
- `[team]`: the team does not exist, the token cannot see it, or it has no members. Suggest checking the team's slug, the name in its URL.
- `[history]`: the history file cannot be read or written, or it is not one this report wrote. Report the message and ask the user to fix or move the file, or name another path.
- `[rate_limit]`: GitHub kept rate-limiting after retries, or asked for a long wait. Suggest rerunning later.
- `[network]`: `gh` could not reach GitHub. Suggest checking the connection and rerunning.
- `[incomplete]`: a search kept returning fewer results than GitHub reported, usually because results changed while it was paging or the search timed out. Suggest rerunning.
- `[api]` saying the listed users or repositories cannot be searched: `USER` or `ORG` does not exist or the token cannot see it. Suggest checking both logins' spelling (the organization's login, not its display name) and that the token can read the organization.
- Any other kind: report the message unchanged.

A `FAILED <type>: <message>` line with no `[<kind>]` is an unexpected failure the script did not classify; it also exits 1. Report it unchanged.

An argument the script refuses is not a `FAILED` line: the script queries nothing, prints nothing on stdout, writes its usage and the reason (such as `argument --org: not a valid GitHub login: 'My Org'`) on stderr, and exits 2. Name the argument and ask the user for a valid login (an organization's login, not its display name), a team slug, a month count from 1 to 36, or a history path outside every skills directory in a folder that exists. `--metric` without a group or a history is refused the same way.
