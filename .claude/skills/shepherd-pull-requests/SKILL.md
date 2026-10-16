---
name: shepherd-pull-requests
description: "Run one merge-shepherd tick from the hub over this repository's open pull requests: fast-forward main, arm auto-merge, merge main into behind branches, judge each red Validation check as a rerun or a defect, and name each merged worktree to retire."
argument-hint: "[--dry-run]"
allowed-tools: ["Bash(python -B .claude/skills/shepherd-pull-requests/scripts/shepherd_pull_requests.py *)", "PowerShell(python -B .claude/skills/shepherd-pull-requests/scripts/shepherd_pull_requests.py *)"]
disable-model-invocation: true
---

# Shepherd pull requests

One tick keeps the open pull requests merging. `.claude/skills/shepherd-pull-requests/scripts/shepherd_pull_requests.py` does every deterministic step, and its docstring lists every line. Your part is the messages, the judgment it leaves you, and the hand-off to `repo-cleanup`.

## 1. Run the tick

From the hub on `main`, in the hub's own session:

```bash
python -B .claude/skills/shepherd-pull-requests/scripts/shepherd_pull_requests.py
python -B .claude/skills/shepherd-pull-requests/scripts/shepherd_pull_requests.py --dry-run
```

The second form lists and judges and writes nothing. From anywhere else, such as a worktree, run only that one; its `HUB` line says the hub is left to the hub's own session. Never rebase or force-push a pull request's branch.

## 2. Act on the lines

- `DEFECT #<number> <branch> leg "<leg>" check "<check>"`: a failure of the change. Message the session whose worktree holds `<branch>`, naming the pull request, the leg, and the check, once per head; it fixes the cause. When no session holds the branch, tell the user. Never fix it here.
- `RERUN_SPENT`: a rerun case on a head already rerun once. Read the run's failed log, `gh run view <run> --log-failed`, and decide: when the failure is plainly the runner's or a service's, not the change's, rerun it with `gh run rerun <run> --failed`; otherwise treat it as a defect. A `DEFECT` whose log shows such a failure is the same call.
- `HOLD #<number> changes <path>`: every other pull request's update waits until that one merges. Say so when you report.
- `PR_FAILED`, `MERGED_FAILED`, or a last line `FAILED <reason>`: report the reason; the next tick tries again.
- `REMAINING 0`: no pull request is open. Stop whatever repeats the tick, such as a scheduled prompt.

`JUDGED`, `RERUN`, `PR`, and `HUB` lines need nothing from you.

## 3. Hand off each merge

Each `MERGED #<number> worktree "<path>" branch <branch>` names a worktree whose pull request merged. First confirm that no live session still works in `<path>` or on `<branch>`, and archive the owning session once it is idle. Then invoke the `repo-cleanup` skill on the hub, from a session that is not inside `<path>`; it proves the merge again before it removes the worktree and deletes the branch.
