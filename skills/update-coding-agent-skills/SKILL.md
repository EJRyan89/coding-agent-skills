---
name: update-coding-agent-skills
description: "Fast-forward the coding-agent-skills clone these skills were deployed from to origin/main, then redeploy every skill with deploy.py --all. Stops before a release that raises the major version until the user passes --cross-major, and over a deployment pinned with deploy.py --at until the user passes --unpin."
argument-hint: "[--cross-major] [--unpin]"
allowed-tools: ["Bash(bash \"${CLAUDE_SKILL_DIR}/scripts/update.sh\" *)", "PowerShell(bash \"${CLAUDE_SKILL_DIR}/scripts/update.sh\" *)"]
disable-model-invocation: true
---

Pull the latest changes into the clone at `{{SOURCE_ROOT}}` and redeploy them. `main` is the supported line and releases are checkpoints on it, so the update follows `main` and stops at a release that raises the major version. Run the update script once, without piping it, through the Bash tool when there is one (in PowerShell, `bash` can be WSL's rather than Git Bash), and report what it printed. Pass after the clone path each of `--cross-major` and `--unpin` that the user started the skill with, and nothing else.

```bash
bash "${CLAUDE_SKILL_DIR}/scripts/update.sh" "{{SOURCE_ROOT}}"
```

The first line of output is the status; act on it, not on the exit code:

- `UP_TO_DATE <sha>` or `UPDATED <old>..<new>`: the clone is on `main` at `origin/main`; a clone found on another branch was switched to `main` and stays there. `UPDATED` is followed by the pulled commits, and by `CROSSED <current>..<target>` when `--cross-major` accepted a release boundary. The deployer's output follows, ending in `DEPLOYED` or `DEPLOY_FAILED <code>`. Report the commit range, the pulled commits, the crossing if any, and the deployer's summary of updated, unchanged, skipped, and removed skills. If the deployer printed `Replaced the deployment pinned with --at <ref> ...`, quote that line: the installation was pinned to another commit and now follows `main`. On `DEPLOY_FAILED`, quote the deployer's error; when it refuses to replace a pinned deployment, nothing was deployed, so report it as `PINNED` below.
- `MAJOR_UPDATE <current>..<target>`: `origin/main` carries a release whose major version is above the installed one (or whose minor version is, while the major is 0). The installed version is the release of the commit `deploy.py check` names as deployed; `<current>` is `untagged` when no release tag reaches that commit, which counts as version 0.0.0, and `unknown` when check names no deployed commit, which any release stops. The update may ask the user to act, as the release notes say. Nothing was merged or deployed. Report both versions and the listed commits, point the user at the release notes for `<target>` on the repository's releases page, and say that rerunning the skill with `--cross-major` applies it. Do not rerun it yourself.
- `PINNED <ref> <commit>`: the installation was deployed with `deploy.py --at <ref>`, which pinned it to `<commit>`, and updating would replace it with `main`. Nothing was fetched or deployed. Report the ref and the commit, and say that rerunning the skill with `--unpin` updates past the pin to `main`, and that until then the installation stays at the pinned commit. Do not rerun it yourself.
- `DIRTY`: tracked files have uncommitted changes, listed below it. Nothing was fetched. Report the files and stop.
- `TAG_SHADOWS_BRANCH`: a tag named `main` or `origin/main`, listed below it as `on origin` or `in the clone`, would stand in for that branch. Nothing was fetched, merged, or deployed. Report the tags and stop; removing one is the user's decision.
- `FETCH_FAILED`, `CHECKOUT_FAILED`, or `NOT_FAST_FORWARD`: nothing was merged or deployed, and the clone is on the branch it was found on. Report what follows the status and stop. `NOT_FAST_FORWARD` lists the commits local `main` has that `origin/main` lacks, or quotes Git's refusal; a `still on main:` line below it means the clone could not be switched back, so report that too.
- `FAILED <reason>`: the clone path holds no `deploy.py` or is not a Git repository, a step failed outright, or a release tag has a version number too long to compare. Nothing was merged or deployed. Report the reason and stop.
- A usage error (exit 2) prints only on stderr: report it and stop.

Never stash, rebase, reset, or force anything to work around a stop, and never rerun the deployer with `--force` or `--force-item`; those decisions are the user's.
