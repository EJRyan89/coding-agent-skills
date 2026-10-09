---
name: update-coding-agent-skills
description: "Fast-forward the coding-agent-skills clone these skills were deployed from to origin/main, then redeploy every skill with deploy.py --all. Stops before a release that raises the major version until the user passes --cross-major."
argument-hint: "[--cross-major]"
allowed-tools: ["Bash(bash \"${CLAUDE_SKILL_DIR}/scripts/update.sh\" *)", "PowerShell(bash \"${CLAUDE_SKILL_DIR}/scripts/update.sh\" *)"]
disable-model-invocation: true
---

Pull the latest changes into the clone at `{{SOURCE_ROOT}}` and redeploy them. `main` is the supported line and releases are checkpoints on it, so the update follows `main` and stops at a release that raises the major version. Run the update script once, without piping it, through the Bash tool when there is one (in PowerShell, `bash` can be WSL's rather than Git Bash), and report what it printed. When the user started the skill with `--cross-major`, pass it after the clone path; pass nothing else.

```bash
bash "${CLAUDE_SKILL_DIR}/scripts/update.sh" "{{SOURCE_ROOT}}"
```

The first line of output is the status; act on it, not on the exit code:

- `UP_TO_DATE <sha>` or `UPDATED <old>..<new>`: the clone is on `main` at `origin/main`. `UPDATED` is followed by the pulled commits, and by `CROSSED <current>..<target>` when `--cross-major` accepted a release boundary. The deployer's output follows, ending in `DEPLOYED` or `DEPLOY_FAILED <code>`. Report the commit range, the pulled commits, the crossing if any, and the deployer's summary of updated, unchanged, skipped, and removed skills. On `DEPLOY_FAILED`, quote the deployer's error.
- `MAJOR_UPDATE <current>..<target>`: `origin/main` carries a release whose major version is above the installed one (or whose minor version is, while the major is 0); `<current>` is `untagged` when no release tag reaches local `main`, which counts as version 0.0.0. The update may ask the user to act, as the release notes say. Nothing was merged or deployed. Report both versions and the listed commits, point the user at the release notes for `<target>` on the repository's releases page, and say that rerunning the skill with `--cross-major` applies it. Do not rerun it yourself.
- `DIRTY`: tracked files have uncommitted changes, listed below it. Nothing was fetched. Report the files and stop.
- `FETCH_FAILED`, `CHECKOUT_FAILED`, or `NOT_FAST_FORWARD`: nothing was merged or deployed. Report Git's error and stop. `NOT_FAST_FORWARD` means local `main` has commits that `origin/main` lacks.
- `FAILED <reason>`: the clone path holds no `deploy.py` or is not a Git repository, or a step failed outright. Nothing was changed. Report the reason and stop.
- A usage error (exit 2) prints only on stderr: report it and stop.

Never stash, rebase, reset, or force anything to work around a stop, and never rerun the deployer with `--force` or `--force-item`; those decisions are the user's.
