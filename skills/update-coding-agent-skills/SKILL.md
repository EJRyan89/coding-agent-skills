---
name: update-coding-agent-skills
description: "Fast-forward the coding-agent-skills clone these skills were deployed from to origin/main, then redeploy every skill with deploy.py --all."
allowed-tools: ["Bash(bash \"${CLAUDE_SKILL_DIR}/scripts/update.sh\" *)", "PowerShell(bash \"${CLAUDE_SKILL_DIR}/scripts/update.sh\" *)"]
disable-model-invocation: true
---

Pull the latest changes into the clone at `{{SOURCE_ROOT}}` and redeploy them. Run the update script once, without piping it, through the Bash tool when there is one (in PowerShell, `bash` can be WSL's rather than Git Bash), and report what it printed:

```bash
bash "${CLAUDE_SKILL_DIR}/scripts/update.sh" "{{SOURCE_ROOT}}"
```

The first line of output is the status:

- `UP_TO_DATE <sha>` or `UPDATED <old>..<new>`: the clone is on `main` at `origin/main`. `UPDATED` is followed by the pulled commits. The deployer's output follows, ending in `DEPLOYED` (exit 0) or `DEPLOY_FAILED <code>` (exit 1). Report the commit range, the pulled commits, and the deployer's summary of updated, unchanged, skipped, and removed skills. On `DEPLOY_FAILED`, quote the deployer's error.
- `DIRTY` (exit 3): tracked files have uncommitted changes, listed below it. Nothing was fetched. Report the files and stop.
- `FETCH_FAILED` (exit 4), `CHECKOUT_FAILED` or `NOT_FAST_FORWARD` (exit 5): nothing was merged or deployed. Report Git's error and stop. `NOT_FAST_FORWARD` means local `main` has commits that `origin/main` lacks.
- Exit 2 is a usage error: report it and stop.

Never stash, rebase, reset, or force anything to work around a stop, and never rerun the deployer with `--force` or `--force-item`; those decisions are the user's.
