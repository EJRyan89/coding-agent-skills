# Releasing

How a release of this repository is cut. Every release is a tag on `main` created through a GitHub release; nothing is built or uploaded, because users install from a clone. Versions below `1.0.0` are tagged as pre-releases.

## Versioning

A version number tells a user what the update asks of them, because `update-coding-agent-skills` fast-forwards an installation and the skills run from a clone rather than from a built artifact. The level of a release is set by the largest demand any change since the last tag makes on the user, judged against the contracts below.

**Contracts**, in the order a break hurts:

1. **Durable user data.** The code-review records, flags, and tracker state under the code-review state directory, and the ownership manifest under `~/.claude/skills`. A format change without an automatic migration is the most expensive break this project can ship.
2. **User-authored files.** Reviewer manifests and the code-review configuration file, which people write by hand.
3. **Skill invocation.** Skill names, their arguments, and the status lines an agent parses, such as `UP_TO_DATE` or `FAILED`.
4. **Deployer operation.** Commands and flags, configured variables, required tool floors, and supported platforms.
5. **Behavior.** Prompt wording, review heuristics, and output no script parses. Behavior is not a contract; the structured record is.

**Levels**, from `1.0.0` on:

- **Patch.** The user does nothing after updating: fixes, documentation, prompt wording, tests, internal refactoring.
- **Minor.** The update may ask for something optional or additive: a new skill or opt-in, a new configured variable that `configure` prompts for, a new platform, a new output line, a manifest version the previous release can still read (`OLDEST_READABLE_VERSION` in `deployer/manifest.py`).
- **Major.** The user must act or loses something: a removed or renamed skill, a renamed argument or status line, a record or configuration format that needs a migration, a dropped platform, a raised tool floor, or a manifest the previous release cannot read.

**Before `1.0.0`**, the minor level carries changes that would be major later, each with its migration or its stated manual step, and the patch level carries fixes only. `1.0.0` is tagged when the deployer runs on macOS and Linux as well as Windows, every contract above is pinned by a test against literal values, and a record or manifest migration has shipped and been exercised in a release.

A change to a contract file, such as `MANIFEST_VERSION`, a `required_vars` list under `deploy-meta/`, `skills/code-review-core/references/review-adapter.schema.json`, a format table in `docs/code-review-operations-contract.md`, or a skill directory name, is what raises the level. Until a validation check holds that rule, which [issue #24](https://github.com/EJRyan89/coding-agent-skills/issues/24) adds alongside the version identity, the person tagging reads the diff since the last tag against the list above.

## Before tagging

1. **Validation and the canary.** `main` is green by construction, since every change arrives through a validated pull request. For a release, also run the `runtime-canary` repository skill against the skills changed since the last tag, so each runtime is seen finding and running them from a deployment, not only passing tests.
2. **Deployability on a fresh machine.** Dispatch the manual `deployable.yml` workflow against `main`:

   ```bash
   gh workflow run deployable.yml --ref main
   ```

   Or use **Actions > Deployable > Run workflow**. Its `claude-version`, `codex-version`, and `copilot-version` inputs default to the versions in the README's table; pass `latest` to try the newest release. The workflow installs the prerequisites and the three runtimes on a fresh GitHub-hosted Windows runner, makes the two Codex Windows settings, configures and deploys into the runner's profile, checks that Codex CLI and Copilot CLI find every adapter and that Claude Code is installed with every deployed skill and agent in place (it cannot list skills without a session, so this is a file-layout check), uninstalls, confirms nothing is left, deploys again, and checks again. It is a manual check, never a required status check, and it starts no model, so it does not show a skill running.

   Read the run's summary page: the tool versions, then one row per runtime and pass, each `PASSED`, `SKIPPED` with the reason (a runtime that will not list skills until it is signed in), or `FAILED`. A green run means every runtime that could list found every adapter and at least one could list. The **deployable-run** artifact holds each command's log, the verify results, and the manifest. Quote the versions and the date in the release notes, and update the README's fresh-runner column and date to match. Installing by hand on a clean Windows environment, one runtime at a time, is the stronger check and was not done for `v0.1.0`; do it when a machine and the time exist, and record the environment in the README's supported-runtimes section either way.
3. **Private-name scan.** Scan the tracked tree against the private list kept outside the repository, one term per line, and expect no hits:

   ```bash
   git grep -n -i -w -F -f <list> -- . | grep -v YourName
   ```

   `YourName` is the documented placeholder in test fixtures. The CI private-reference check matches only generic patterns, and adding a specific name to it would publish the name, so the list never enters the repository. Any hit is a prompt to read the line, not a verdict; fix a real one through a pull request before tagging.
4. **Statements that carry a version.** The README's supported-runtimes table names the versions tested and where they were tested; `SECURITY.md` names `main` as the supported line and releases as checkpoints. Confirm both still describe the truth for this release.
5. **Secret scanning.** GitHub's secret scanning and push protection are on for the public repository; confirm they still are in the repository's security settings.

## Tagging

Write the notes to a file and create the release from `main`:

```bash
gh release create vX.Y.Z --target main --prerelease --title "vX.Y.Z" --notes-file <file>
```

The notes say what the README says: what is included, the supported platform, the runtime versions tested and where, and what was deferred. They open with the level of the release and the action, if any, the update asks of the user, as [Versioning](#versioning) defines them. Drop `--prerelease` at `1.0.0`.

## After tagging

1. Deploy from the hub on `main` and confirm `python deploy.py verify` finds every adapter.
2. Start `update-coding-agent-skills` from a runtime and confirm it reports `UP_TO_DATE`, which exercises the rendered source path and the fetch together. A release that raised the breaking component makes the skill stop at `MAJOR_UPDATE` on every installation behind it until the user passes `--cross-major`, so its notes must say what the user has to do.
3. Retire any worktree the release work used.

The first release, `v0.1.0`, was tagged on 2026-10-04; the history of how the repository was prepared for publication is in the Git history before that tag.
