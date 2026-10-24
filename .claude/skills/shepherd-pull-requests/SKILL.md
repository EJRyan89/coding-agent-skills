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

The second form lists and judges and writes nothing. From anywhere else, such as a worktree, run only that one; its `HUB` line says the hub is left to the hub's own session. Never rebase or force-push a pull request's branch, and never update a behind pull request the tick left `next-tick`: it updates one at a time, since an update cancels the run in flight on the branch it updates.

The tick admits only pull requests whose head branch is in this repository. One from any other repository, such as a fork, is never armed, judged, updated, or rerun, and holds no other pull request's update; merging it stays the maintainer's call. Never arm one by hand either.

## 2. Act on the lines

- `NOT_ADMITTED #<number> <branch> head <commit> from another repository`: a pull request the tick does not admit. Tell the user once per head, naming the pull request; never message a session about it or act on it.
- `DEFECT #<number> <branch> leg "<leg>" check "<check>"`: a failure of the change. Message the session whose worktree holds `<branch>`, naming the pull request, the leg, and the check, once per head; it fixes the cause. When no session holds the branch, tell the user. Never fix it here.
- `OUT_OF_STEP #<number> <branch>`: GitHub missed a push to `<branch>`, so the pull request records an older head and can be neither judged nor updated. Message the session whose worktree holds `<branch>` to merge `origin/main` and push, once per head; when no session holds it, tell the user, since closing and reopening the pull request is the maintainer's call.
- `CONFLICT #<number> <branch>`: the pull request conflicts with `main`, which only its owner can resolve. Message the session whose worktree holds `<branch>` to merge `origin/main`, resolve the conflict, and push, once per head; when no session holds it, tell the user.
- `RERUN_SPENT`: a rerun case on a head already rerun once. Read the run's failed log, `gh run view <run> --log-failed`, and decide: when the failure is plainly the runner's or a service's, not the change's, rerun it with `gh run rerun <run> --failed`; otherwise treat it as a defect. A `DEFECT` whose log shows such a failure is the same call.
- `HOLD #<number> changes <path>`: every other pull request's update waits until that one merges. Say so when you report.
- `HELD #<number> <branch> "<reason>"`: a `Shepherd-hold: <reason>` line in the pull request's body keeps it unarmed. Say so when you report; deleting the line releases it, as "An orchestrator and a shepherd" in `docs/parallel-sessions.md` says.
- `HUB "<path>" not fast-forwarded: <reason>` from a tick in the hub: the hub is off `main`, has a tracked change, diverged, or could not fetch. Tell the user the reason; the next tick tries again.
- `PR_FAILED`, `MERGED_FAILED`, or a last line `FAILED <reason>`: report the reason; the next tick tries again.
- `REMAINING 0`: no pull request is open. Stop whatever repeats the tick, such as a scheduled prompt.

`JUDGED`, `RERUN`, `PR`, and any other `HUB` lines, such as one from a linked worktree, need nothing from you, nor do `REPOSITORY <owner>/<name>`, the repository the tick reads, and `DRY_RUN`, which opens a `--dry-run` tick.

## 3. Hand off each merge

Each `MERGED #<number> worktree "<path>" branch <branch>` names a worktree whose pull request merged, matched by its branch or by its commit when the session pushed under another name, and each local branch to delete: one `branch <branch>` for the worktree's own, and another for the pull request's when one of that name exists. First confirm that no live session still works in `<path>` or on a named branch, and archive the owning session once it is idle. Then invoke the `repo-cleanup` skill on the hub, from a session that is not inside `<path>`; it proves the merge again before it removes the worktree and deletes each branch, and offers one it cannot prove, such as an unpushed worktree branch.
