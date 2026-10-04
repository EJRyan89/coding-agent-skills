# Releasing

How a release of this repository is cut. Every release is a tag on `main` created through a GitHub release; nothing is built or uploaded, because users install from a clone. Versions below `1.0.0` are tagged as pre-releases.

## Before tagging

1. **Validation and the canary.** `main` is green by construction, since every change arrives through a validated pull request. For a release, also run the `runtime-canary` repository skill against the skills changed since the last tag, so each runtime is seen finding and running them from a deployment, not only passing tests.
2. **Deployability on a fresh machine.** Dispatch the manual deployability workflow (see the issue that adds it, or `deployable.yml` once it exists). It installs the prerequisites on a fresh GitHub-hosted Windows runner, deploys into a throwaway home, checks Codex CLI and Copilot CLI discovery, uninstalls, and deploys again. Installing by hand on a clean Windows environment, one runtime at a time, is the stronger check and was not done for `v0.1.0`; do it when a machine and the time exist, and record the environment in the README's supported-runtimes section either way.
3. **Private-name scan.** Scan the tracked tree against the private list kept outside the repository, one term per line, and expect no hits:

   ```bash
   git grep -n -i -w -F -f <list> -- . | grep -v YourName
   ```

   `YourName` is the documented placeholder in test fixtures. The CI private-reference check matches only generic patterns, and adding a specific name to it would publish the name, so the list never enters the repository. Any hit is a prompt to read the line, not a verdict; fix a real one through a pull request before tagging.
4. **Statements that carry a version.** The README's supported-runtimes table names the versions tested and where they were tested; `SECURITY.md` says which release is supported. Confirm both still describe the truth for this release.
5. **Secret scanning.** GitHub's secret scanning and push protection are on for the public repository; confirm they still are in the repository's security settings.

## Tagging

Write the notes to a file and create the release from `main`:

```bash
gh release create vX.Y.Z --target main --prerelease --title "vX.Y.Z" --notes-file <file>
```

The notes say what the README says: what is included, the supported platform, the runtime versions tested and where, and what was deferred. Drop `--prerelease` at `1.0.0`.

## After tagging

1. Deploy from the hub on `main` and confirm `python deploy.py verify` finds every adapter.
2. Start `update-coding-agent-skills` from a runtime and confirm it reports `UP_TO_DATE`, which exercises the rendered source path and the fetch together.
3. Retire any worktree the release work used.

The first release, `v0.1.0`, was tagged on 2026-10-04; the history of how the repository was prepared for publication is in the Git history before that tag.
