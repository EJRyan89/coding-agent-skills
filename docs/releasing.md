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

A change to a contract file is what raises the level, and validation holds that rule. `tests/run_validation.py` finds the last tag with `git describe --tags --abbrev=0 origin/main` and compares these contract values at that tag with the working tree: `MANIFEST_VERSION` and `OLDEST_READABLE_VERSION` in `deployer/manifest.py`, each `required_vars` list under `deploy-meta/`, each schema under `skills/code-review-core/references/`, the format tables under "Formats" in `docs/code-review-operations-contract.md`, the skill directory names under `skills/`, and the tool floors in `deployer/tools.py`. While one differs and no upgrade note since that tag names it, validation fails, naming the item and what the entry must say: its level in the words of the list above and the user action or none. An entry whose level is not one of those words fails too. A pull request writes its entry under `## Upgrade note` in its body, never in [upgrade-notes.md](upgrade-notes.md), and the repository squash-merges with the body as the commit message, so the notes since the tag are the entries in the commit messages since it. In a pull request, validation reads the body, which CI writes from the event payload to a file and passes with `--pr-body`, and the commits merged into `origin/main`, not the branch's own commits, since the body is what becomes the squash commit's message; a local run without `--pr-body` reads the commits on `HEAD`, the branch's included. Editing the body reruns the check. With no tag reachable from `origin/main` nothing has been released and the check passes; a missing `origin/main` or a shallow clone fails it with the fetch command that fixes it. Skill arguments, status lines, and deployer flags have no contract file, so the pull request that changes one writes its entry without being asked.

## Before tagging

1. **Validation and the canary.** `main` is green by construction, since every change arrives through a validated pull request. That rests on the branch protection [CONTRIBUTING.md](../CONTRIBUTING.md#how-main-is-protected) states; run `python tools/branch_protection.py`, which reads it through `gh api`, and expect `PROTECTED`. For a release, also run the `runtime-canary` repository skill against every shipped skill changed since the last tag, at most three to a run, so each runtime is seen finding and running them from a deployment, not only passing tests.
2. **Deployability on a fresh machine.** Dispatch the `deployable.yml` workflow against `main`:

   ```bash
   gh workflow run deployable.yml --ref main
   ```

   Or use **Actions > Deployable > Run workflow**. Its `claude-version`, `codex-version`, and `copilot-version` inputs default to the versions in the README's table; pass `latest` to try the newest release. The workflow installs the prerequisites and the three runtimes on a fresh GitHub-hosted Windows runner, makes the two Codex Windows settings, configures and deploys into the runner's profile, checks that Codex CLI and Copilot CLI find every adapter and that Claude Code is installed with every deployed skill and agent in place (it cannot list skills without a session, so this is a file-layout check), uninstalls, confirms nothing is left, deploys again, and checks again. It is never a required status check, and it starts no model, so it does not show a skill running. It also runs every Tuesday, the day after `validate.yml`'s weekly run, once on the README's versions and once on the latest release of every runtime, so a runtime release that breaks a fresh install shows within a week; a scheduled failure is a signal to read, not a gate. A release still dispatches its own run, because the README's fresh-runner column and date record the last dispatched release run, never a weekly one.

   Read the run's summary page: the tool versions, then one row per runtime and pass, each `PASSED`, `SKIPPED` with the reason (a runtime that will not list skills until it is signed in), or `FAILED`. A green run means every runtime that could list found every adapter and at least one could list. The **deployable-run-requested** artifact (a scheduled run's are `deployable-run-readme` and `deployable-run-latest`) holds each command's log, the verify results, and the manifest. Quote the versions and the date in the release notes, and update the README's fresh-runner column and date to match. Installing by hand on a clean Windows environment, one runtime at a time, is the stronger check and was not done for `v0.1.0`; do it when a machine and the time exist, and record the environment in the README's supported-runtimes section either way.
3. **Private-name scan.** Scan the tracked tree against the private list kept outside the repository, one term per line, and expect no hits:

   ```bash
   git grep -n -i -w -F -f '<list>' -- . | grep -v YourName
   ```

   `YourName` is the documented placeholder in test fixtures. The CI private-reference check matches only generic patterns, and adding a specific name to it would publish the name, so the list never enters the repository. Any hit is a prompt to read the line, not a verdict; fix a real one through a pull request before tagging.
4. **Statements that carry a version.** The README's supported-runtimes table names the versions tested and where they were tested; `SECURITY.md` names `main` as the supported line and releases as checkpoints. Confirm both still describe the truth for this release.
5. **Security settings.** The `python tools/branch_protection.py` run in step 1 also confirms that secret scanning, push protection, private vulnerability reporting, and Dependabot security updates are enabled and that the default workflow token is read-only, and prints each setting as it found it; `PROTECTED` covers them.
6. **Validation legs.** In the release pull request, run `python -B tools/record_job_seconds.py`. It reads the logs of the latest successful `validate` run on `main` through `gh run view --log` and rewrites `tests/validation/job_seconds.json` with the seconds each job took, which is how `tests/run_validation.py` deals the jobs across the workflow's four legs. Pass a run's ID to read that run instead; a run that did not finish successfully is refused. Commit the table, and after the release pull request merges, confirm its `validate` run's four legs took about the same time.
7. **Upgrade notes.** In the release pull request, run `python tools/release_notes.py vX.Y.Z --write`. It writes the `## vX.Y.Z` section of [upgrade-notes.md](upgrade-notes.md) from the upgrade notes of the commits since the last tag, newest first, each naming its pull request from the squash commit's title. Read the section against the milestone's `Shipped:` list, add any obligation the milestone records as an entry, and commit it; the release audit in step 8 reads it, and the release notes open with it.
8. **Release audit.** Run the [`audit-repository`](../.claude/skills/audit-repository/SKILL.md) repository skill's `release` mode, which writes one brief per area changed since the last tag for a read-only reader. It asks whether the release keeps what it says: each change matches its issue and its upgrade note, no contract or document contradicts the code, every changed behavior has its test, and every grant a changed skill declares is used. A confirmed defect is fixed before the tag. Drift and polish are filed into the next milestone, and the release notes name each as deferred. A trust-boundary finding goes to a private security advisory, as [SECURITY.md](../SECURITY.md) says, never to a public issue.

## Tagging

Write the notes to a file and create the release from `main`:

```bash
gh release create vX.Y.Z --target main --prerelease --title "vX.Y.Z" --notes-file '<file>'
```

The notes open with the release's section of [upgrade-notes.md](upgrade-notes.md), which [Before tagging](#before-tagging) wrote: the level of the release, the largest any entry names, and the action, if any, each asks of the user, as [Versioning](#versioning) defines them. The notes then say what the README says: what is included, the supported platform, the runtime versions tested and where, and what was deferred. Carry the release milestone's description into them: its `Shipped:` list of issues and the pull requests that closed them, and any release-note obligation it records. Drop `--prerelease` at `1.0.0`.

### Release notes

The GitHub release notes are the record of what changed between tags; the repository keeps no `CHANGELOG.md`. Since `main` is the supported line and every installation follows it, the notes are where a user reads what an update brought and what it asks of them, and `MAJOR_UPDATE` points the user at them.

A release-note obligation that a pull request records belongs in its body's `## Upgrade note`, and one a milestone records is added to the generated section in the release pull request, so each reaches [upgrade-notes.md](upgrade-notes.md) and the notes with the rest. The `v0.2.0` obligations are recorded there: updating removes `init-ai-config`, and `update-coding-agent-skills` stops at `MAJOR_UPDATE` on every `v0.1.x` installation until the user starts it again with `--cross-major`.

## After tagging

1. Deploy from the hub on `main` and confirm `python deploy.py verify` finds every adapter.
2. Start `update-coding-agent-skills` from a runtime and confirm it reports `UP_TO_DATE`, which exercises the rendered source path and the fetch together. A release that raised the breaking component makes the skill stop at `MAJOR_UPDATE` on every installation behind it until the user passes `--cross-major`, so its notes must say what the user has to do.
3. **Rotation audit.** Run the `audit-repository` skill's `rotation` mode, which names the area its rotation record shows read whole least recently (the deployer, the code review skills, the other skills, or the repository's infrastructure and documents) and writes that area's whole-area brief, so no area goes unread for more than four releases. Its findings, with the issues the new milestone carries, feed the next milestone; it holds nothing. Record the area for the new tag with `rotation --record`, through a pull request, and note on the milestone which areas both audits read.
4. Retire any worktree the release work used.

The first release, `v0.1.0`, was tagged on 2026-10-04; the history of how the repository was prepared for publication is in the Git history before that tag.
