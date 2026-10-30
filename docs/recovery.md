# Recovery

The deployer changes your home directory only through a journaled transaction, so an interrupted or failed deployment is put right by the next run. This page explains the state it keeps, what that next run does, and what to do in the few cases it stops and asks you to decide. Every deployer message that sends you here names the section to read. When a message alone does not show the cause, rerun the command with `--debug`, or with `DEPLOYER_DEBUG=1` set, to print the traceback after it. On Linux, run each `python deploy.py` command on this page as `python3 deploy.py`, as [Installation](installation.md#on-linux) says; the deployer's own messages already name it so there.

## Where the deployer keeps its state

| Path | What it is |
| --- | --- |
| `~/.claude/skills/<name>` | A deployed skill or shared asset. |
| `~/.agents/skills/<name>` | A deployed runtime adapter for Codex and Copilot CLI. |
| `~/.claude/agents/<name>.md` | A deployed Claude Code subagent definition. |
| `~/.claude/skills/.deploy-manifest.json` | The ownership manifest: which source deployed each item, its hash, the direct skills (`skill_deps`), shared assets (`shared_deps`) and agents (`agent_deps`) each skill was deployed with, the checkout and commit each source was deployed from (with the ref, for a deployment made with `--at`), and the last committed run ID. |
| `~/.claude/deployer/config/<id>.config` | The configuration for one source, written by `python deploy.py configure`. |
| `~/.claude/deployer/staging/<run-id>/` | One run in progress: its rendered files and its journal, `journal.jsonl`. |
| `~/.claude/deployer/.deploy.lock.d/` | The lock held by the deployment that is running. |
| `<root>/<name>.deploying-bak` | The previous copy of an item while a run replaces or removes it. |
| `<root>/.backups/<run-id>/<name>` | A permanent backup of a copy a run replaced with `--force` or `--force-item`. |
| `deploy-at-<random>/` in the system temporary directory | The files of the commit a `--at` run deploys, exported for that run and removed when it ends; one left behind after the process was killed can be deleted. |

`<root>` is whichever of `~/.claude/skills`, `~/.agents/skills`, and `~/.claude/agents` holds the item. A `<run-id>` is the time the run started, in UTC, as `YYYYMMDD-HHMMSS`, followed by four random hexadecimal characters, such as `20261006-214501-3fa2`; an older deployer's run IDs give the local time instead.

## Interrupted deployments

Before a run changes an item, it writes the change to its journal: a `backup` line when it moves the existing copy to `<name>.deploying-bak`, an `install` line when it moves the new copy into place, and a `preserve` line when it moves a backup to `.backups/<run-id>/`. Only after every item is in place does it record its run ID in the manifest, which commits the run. It then removes the transient backups it no longer needs, moves the ones it keeps to `.backups/`, and deletes its staging directory. A transient backup is removed only while it matches the hash the run recorded before moving it. One that differs was changed after the run read it, for example by an editor saving the item just before the move, so it is kept in `.backups/<run-id>/` as a forced replacement's would be, listed under `BACKED UP`, with a `WARNING` naming it.

An older supported manifest is upgraded in memory on read and saved only with an atomic manifest commit. A dry run or refusal never writes the upgrade; an interrupted deployment before the commit rolls back its items and leaves the old manifest, and one after the commit finishes against the upgraded manifest. When an older skill entry has no `skill_deps`, its historical dependencies cannot be recovered from current source metadata: it conservatively records the other skills its source owns. A skipped or preserved skill then keeps those skills, their assets and agents, and their runtime adapters until it is restored or replaced by an unmodified selected deployment, which records its exact current dependencies. Dependencies already removed by an earlier deployment are not recreated by the migration.

A run that fails with an error or is stopped with Ctrl+C reconciles its own journal before it exits. If it cannot, or the process is killed outright, for example by closing the terminal, its staging directory remains and the next run reconciles it before deploying anything:

- A run the manifest records as committed is completed: each installed item is checked against the hash in the journal, kept backups are moved to `.backups/<run-id>/`, and the other transient backups are deleted if they still match the journal's hash, or kept in `.backups/<run-id>/` with a `WARNING` if they do not.
- A run that was not committed is rolled back: each item it installed is removed if it still matches what it installed, and each `<name>.deploying-bak` is moved back into place.

Either way, the staging directory is then deleted and the deployment you asked for continues. You do not need to do anything. Recovery repeats safely: if you press Ctrl+C while it runs, or it stops on an unexpected error, the run exits without deploying further, releases the lock, and keeps the journal, and the next deployment finishes the recovery. This applies both to recovery before a new deployment and to recovery of a failed deployment's own journal, whether rolling back an uncommitted run or completing one already committed. Ctrl+C during either recovery exits 130 with `Cancelled during recovery`; an unexpected recovery error is named, followed by `Recovery stopped before it finished; the next deployment retries it before deploying anything.`, and exits 1. Neither prints a traceback unless debug output was requested. Either way, a line for each run whose journal remains says what is installed:

- `Run <run-id> was committed to the manifest, so its changes are installed and the manifest holds them` means the run had finished deploying when recovery stopped; the next deployment only finishes its cleanup, deleting or keeping its transient backups as above.
- `Run <run-id> was not committed to the manifest, so the manifest does not hold its changes` means the run stopped before its commit; the items it installed may still be in place, and the next deployment rolls them back.

A dry run changes nothing, so it recovers nothing either. While an interrupted run is waiting, `--dry-run` says `Pending recovery: the next deployment will recover run <run-id>` and stops before planning, because recovery changes what is installed and a preview made before it would be wrong; leave the `.deploying-bak` files where they are. Deploy to recover and continue. If it instead says a run `cannot be recovered automatically`, the next deployment would stop too; see [When recovery fails](#when-recovery-fails).

## When recovery fails

Recovery stops instead of guessing when what it finds on disk does not match the journal: an item changed after the run installed it, a backup is missing, or an item and its `.deploying-bak` both exist. It then refuses to deploy, keeps the journal, and prints a `WARNING` line for each item it could not reconcile, followed by the run ID, whether that run was committed, and its staging directory. Other runs are still recovered. The report ends with `ERROR: Recovery failed, so this deployment stopped before deploying anything.`: the deployment you asked for changed nothing beyond recovery, but a run reported as committed is installed, and its items stay as they are.

A file recovery cannot read, move, or delete, usually because an editor or an antivirus scanner holds it open, stops it the same way, with `ERROR: Recovery of run <run-id> failed at <path>` and the reason. Close whatever holds that path and rerun; recovery repeats safely, picking up where it stopped. Reconcile by hand only if it fails again.

To reconcile a run by hand:

1. Do not rerun with `--force`; it would replace items whose correct state you have not checked yet.
2. Open the run's `journal.jsonl`. Each line names an `item` and a `root`: no `root` means `~/.claude/skills`, `agents` means `~/.agents/skills`, and `claude-agents` means `~/.claude/agents`. The items the `WARNING` lines name are the ones to look at.
3. If the run was committed, keep each item as it is. Move its `<name>.deploying-bak` to `.backups/<run-id>/<name>` in the same root if you want to keep the previous copy, or delete it once you have checked you do not need it.
4. If the run was not committed, put back what it replaced: move the current `<name>` out of the root, then rename `<name>.deploying-bak` to `<name>`. Moving it out of the root matters, because Claude Code, Codex, and Copilot would load anything left inside it.
5. When no `.deploying-bak` remains in any of the three roots, delete the run's staging directory, `~/.claude/deployer/staging/<run-id>/`.
6. Run `python deploy.py --all --dry-run`. An item whose content no longer matches the manifest is reported as modified and left alone; replace it with `--force-item <name>` when you are sure, which keeps a backup. The name is the one the report shows; an agent may also be named without its `.md`.

A journal line that cannot be parsed stops recovery the same way, with `Malformed journal entry`, and is reconciled by the same steps. The one exception is a last line with no newline at its end. The deployer writes each line in full, newline included, before the change it records, so such a line was cut off when the run was stopped, before its change began. Recovery drops it with a `WARNING` that quotes it and proceeds, provided every line before it is valid; a bad line anywhere else still stops recovery.

## Backups

A transient backup, `<name>.deploying-bak`, exists only while a run is replacing or removing that item, and recovery resolves it. One left behind with no staging directory to explain it makes the next deployment of that item refuse. Compare it with the item beside it, keep whichever copy you want, move the other out of the root, and rerun.

A permanent backup, `.backups/<run-id>/<name>`, is the copy a forced replacement overwrote. The deployer never deletes these. To restore one, move the current item out of the root and copy the backup into its place. The next deployment reports the restored item as modified and leaves it until you replace it with `--force-item <name>`. A run refuses to write a permanent backup over one that already exists; move the existing one out of the root after checking it, and rerun.

## The deployment lock

`~/.claude/deployer/.deploy.lock.d/` exists while a deployment runs, so two cannot run at once. Its `info.json` records the process ID and start time of the deployment that holds it; on Linux the start time is the one `/proc` reports, and a holder whose start time `/proc` does not show counts as running. A lock whose process has exited, or whose process ID now belongs to another program, is stale, and the next run reclaims it with a warning. Reclaiming moves the lock aside to `~/.claude/deployer/.deploy.lock.stale.<pid>` and checks that what it moved is the lock it judged stale; if another deployment reclaimed it first and holds a fresh one, the run moves that lock back and stops with `contention`, so retry. In the rare case it cannot move it back, the message names the `.deploy.lock.stale.<pid>` directory; delete it once no deployment is running. A reclaim that fails after moving the lock aside deletes that directory, or names it when it cannot. A later reclaim that finds one left behind stops and names it rather than moving the lock; delete it once no deployment is running, and rerun.

A run stopped with Ctrl+C while it takes the lock deletes the lock it was creating, so only a run killed outright at that moment leaves one without `info.json`. The deployer refuses only when it cannot tell: the lock has no readable `info.json`, or its process is running but cannot be confirmed to be the deployment that took the lock. Check that no `python deploy.py` process is running, for example with `Get-Process python` in PowerShell, or `pgrep -af deploy.py` on Linux, then delete the lock directory and rerun. Removing it while a deployment is running would let a second one interleave with it.

## Ownership held by another source

Each repository you deploy from is a source with an ID in its `source.json`, and the manifest records which source deployed each item. A source refuses to replace an item another source owns, which keeps two clones from overwriting each other.

When the other source is an earlier ID of the same repository, or a clone you no longer deploy from, take its items over:

```bash
python deploy.py --migrate-from '<source-id>'
```

This changes no installed item and rewrites only the manifest, where it moves ownership of every item both sources deploy, provided each still matches the hash the old source recorded, and prints them under `MIGRATED`. Then deploy as usual. If an item is missing or has been changed since the old source deployed it, migration refuses without moving anything; restore that copy, for example by deploying from the old source again, and rerun it. `--migrate-from` deploys nothing, so it refuses `--dry-run`, `--all`, `--include`, `--force`, `--force-item`, `--canary-home`, `--at`, and `--unpin` as a command-line error (exit 2), rather than ignoring them; give those to the deployment that follows. The source IDs the manifest knows are listed under `sources` in `~/.claude/skills/.deploy-manifest.json`.

When both sources are still in use, do not migrate: the other source's next deployment would refuse in turn. Deploy the item from only one of them, by deselecting it in the other and redeploying that one.

A name one source deploys as a skill and another as a shared asset cannot be migrated, because it would have to be both. `--migrate-from` moves the other items both sources deploy and leaves that name with the source that deployed it, and the next deployment refuses it as a collision, naming that source. Rename it in one source, or stop deploying it from the other.

On Windows two names that differ only in case, such as `Guide.md` and `guide.md`, are one file, so they are one item. A deployment that would install a case variant of an item any source owns refuses as a collision, naming both. Rename it in one source, or stop deploying it from the other. A manifest written before this refusal can record both names, one under each source; the source that stops deploying its name then reports it under `DROP OWNERSHIP` and leaves the file to the other.

## Deploying from another checkout

The manifest also records the checkout each source was deployed from. Two clones of one repository share its source ID and its configuration, so a deployment from the second would silently take over every item the first deployed: it would point the installed skills, and `update-coding-agent-skills`, at the second clone and remove whatever the second lacks. A deployment from any checkout other than the recorded one therefore refuses before it changes anything, naming both paths.

If you deployed from the wrong clone, deploy from the recorded one instead. If this checkout replaces the recorded one, because you moved or re-cloned the repository, take the source over:

```bash
python deploy.py --all --take-over-source
```

It prints the recorded checkout and how many items the source owns, then deploys as usual; when the deployment commits, the manifest records this checkout, and later deployments from it need no flag. `--take-over-source` also works with `--migrate-from`, and records this checkout even when there is nothing to migrate. It cannot be combined with `--dry-run`, which never refuses the recorded checkout because it reads the source without claiming it, so the flag has nothing to pass there, or with `--canary-home`, whose throwaway home is empty or one an earlier canary deployment marked. A linked worktree is refused whatever the flag; deploy from the main checkout.

## Deploying an earlier commit

If an update brings a regression, deploy the last release, or any earlier commit, without moving the clone off `main`:

```bash
python deploy.py --all --at '<tag-or-commit>'
```

`--at` takes a tag, a branch, or a commit this clone already holds; the deployer does not fetch, so run `git fetch --tags origin` first if Git does not know the name. It exports that commit's files to a throwaway directory and deploys them with the deployer as it is at the clone's `HEAD`, never the commit's own, so the selection, the checks, and the report are the ones you know. The clone's branch, `HEAD`, and working tree do not change, and the manifest still records the clone as the source, so later deployments from it need no flag. `python deploy.py check` then names the deployed commit with `pinned with --at` and the ref. Preview it first with `--dry-run`, which changes nothing; it also works with `--canary-home`.

A ref that starts with `-`, holds a character outside letters, digits, and `. _ / @ { } ~ ^ + -`, runs past 255 characters, names no commit, or whose `source.json` carries another source ID is refused before anything changes; a ref Git cannot read, such as a broken ref or one in a checkout whose configuration Git cannot parse, is refused with Git's reason rather than as a ref to fetch; a change of source ID needs an explicit migration. The character rule keeps a ref a branch, a tag, a commit name, or a revision over them such as `main~2`, and refuses an object selector such as `HEAD:path` as a command-line error (exit 2) before Git runs. So is a commit whose tree holds a symbolic link, a hard link, or a special file, or a file the extraction would place outside the throwaway directory; the refusal names the file. `--at` needs the Python floor in [Installation](installation.md#requirements).

Report the regression with the commit range between the two deployments. The pin holds until you leave it: a deployment without `--at`, with `--force` or `--force-item` or from the selection prompt, refuses before it changes anything, naming the pinned ref and commit, unless you pass `--unpin`. A deployment at the pinned commit, under any name for it, keeps the pin and the ref as you wrote it, unless you pass `--unpin`. So does another `--at`, which moves the pin once you add `--unpin`, as in `python deploy.py --all --at <other-ref> --unpin`, except over the update skill's own pin at a release, described below, which it moves without `--unpin`. The uninstall removes the pin with the items. The deployer holds the pin whichever commit you pinned, because it always runs as it is at the clone's `HEAD`. `update-coding-agent-skills` stops at your pin with `PINNED <ref> <commit>` and deploys nothing: following `main`, before fetching, and following releases, its default, after the fetch, unless your pin is already at the release it would deploy, which it then deploys with your pin left yours. Its own deployment of a release is a pin that names the tag in full, `--at refs/tags/<tag>`, which it moves to each later release; a pin with the tag's short name, as above, is yours. Pinning the commit of the update's own pin by hand, with the tag's short name or the commit, makes the pin yours too: over the update's pin, a deployment at its commit records the ref it names. When Git cannot read the release tags reaching a pin named `refs/tags/<tag>`, the deployer cannot tell whose it is, so a run that would replace it refuses before it changes anything, quoting Git's reason; put right what Git reports, then retry. When `check` prints nothing, prints only its error, or names the deployed commit or the pin in a form the update cannot read, the update ends with `FAILED` before fetching, since it cannot tell whether the installation is pinned; a `check` that exits 1, as for a missing tool, but names the deployed commit does not stop it. When `check` does not name the pin, the update fetches and fast-forwards the clone, on either channel, and the deployer's refusal ends it with `DEPLOY_PINNED <ref> <commit>`. A copy of the skill deployed from a commit that predates that stop fetches and fast-forwards the clone instead, then ends with `DEPLOY_FAILED` and the deployer's refusal, leaving the pinned deployment in place.

To move back to `main`, run `python deploy.py --all --unpin` from the clone, or, when the skill stopped with `PINNED` or `DEPLOY_PINNED`, start it again with `--unpin`. Either replaces the pinned deployment and says so: the deployer's report prints `Replaced the deployment pinned with --at <ref> to <commit>.` after the deployed items and any backups, before the Run ID and Manifest lines, and the skill reports that line.

## A tool that does not finish

Every tool the deployer runs, such as Git Bash, ShellCheck, and PowerShell checking the rendered skills, git reading the checkout, and the version probes of `check` and `verify` but `pdftotext`'s, runs under one deadline, `TOOL_DEADLINE_SECONDS` in `deployer/platform_support.py`. It is minutes long, so a slow but working tool on a busy machine still finishes; a tool that reaches it is almost always stuck. The deployer then ends the tool and every process it started (on Linux, every one that has not left the tool's process group), and stops with `ERROR: <tool> did not finish within <seconds> seconds`, followed by the tool's path.

What that leaves depends on when it happened:

- While the deployment was checking the rendered skills, or reading the checkout, it had changed nothing yet. It refuses as a failed validation does, releases the lock, and leaves no journal, so you can retry at once.
- A tool that times out after the deployment began changing items fails it like any other error: the run reconciles its own journal before it exits, as [Interrupted deployments](#interrupted-deployments) describes, and releases the lock.
- `check` and `verify` change nothing, and stop with the same message.

Two waits have limits of their own, and neither stops the command:

- `check`'s version probe of each `pdftotext` it finds, the same probe every PDF extraction runs, has a few seconds, `PROBE_SECONDS` in `skills/ejryan89-skill-core/scripts/pdf_tool.py`. A probe that reaches it ends the `pdftotext` it started, but not a process that program started, because the probe runs outside a process tree. `check` passes that copy over as `version probe did not finish within <seconds> seconds`, reports the parser `UNUSABLE` when no copy passes, and carries on, since PDF review is optional.
- `verify`'s listing of each runtime's skills has its own answer limit, `DEFAULT_TIMEOUT` in `deployer/discovery.py`, and is held in a process tree like the tools above, so a listing that reaches it ends every process the runtime started. `verify` reports that runtime as `Cannot list its skills: no answer within <seconds> seconds`, goes on to the next runtime, and fails at the end, naming it. Codex's second probe, `codex debug prompt-input`, has the same limit, and one that reaches it is reported as `Listed its skills, but cannot read which ones it offers the model (codex debug prompt-input): no answer within <seconds> seconds`, so the command to run by itself is the one the message names.

Run the tool named in the message by itself, such as `shellcheck --version`, to see whether it hangs there too. An antivirus scan of a freshly installed tool, a prompt the tool shows only in a window of its own, or a broken install are the usual causes; repair or reinstall it as [Installing the tools](installation.md#installing-the-tools) says, then retry.

## Configuration that no longer reads

`python deploy.py configure` writes each source's configuration, and every deployment reads it strictly. When an update removes a variable the file still sets, deployment stops, names the key, and asks you to run `python deploy.py configure`, which drops that key with a `NOTE`, keeps every other value, and saves the file. If the file no longer parses for another reason, for example because it was edited by hand, deployment stops and names the problem. `configure` reads the same file, so start from an empty one:

```bash
python deploy.py configure --reset
```

A directory value that no longer exists or is not absolute is rejected too; run `python deploy.py configure` and enter the new path.

## A state file that does not read

Every run reads the manifest, the source's configuration, the journal of each run left in `~/.claude/deployer/staging/`, and, when another run holds the lock, its `info.json`. A file the deployer cannot read is refused by name rather than guessed at: one that is not JSON, or holds what the deployer never writes, such as a number too long to read or lists nested too deep, is named as malformed, and a folder or anything else that is not a file where one belongs is named with `is not a file`. An earlier release wrote its state without refusing a lone surrogate, a character no UTF-8 file can hold; the deployer now never writes one and does not read one, so a manifest holding one is named with `holds a lone surrogate`, and a journal or lock file holding one is refused as malformed, each put right by the step below. Nothing is planned or changed until it is put right, so a dry run never promises a deployment the commit could not make.

- The manifest, `~/.claude/skills/.deploy-manifest.json`: put back a copy you trust if you have one. Otherwise move it out of `~/.claude/skills` and run `python deploy.py --all --dry-run`. Without a manifest, an installed item identical to what the source renders is listed under `ADOPT`, and one that differs under `CONFLICT` and left alone, so nothing you changed is replaced unless you name it with `--force-item`. What other sources recorded goes with the manifest, so deploy from each of them again. A manifest whose schema version is newer than the deployer reads was written by a newer checkout, and nothing is wrong with it: update this checkout with `git pull`, or deploy from the checkout that wrote it.
- The configuration: move it aside and run `python deploy.py configure`, which writes a new one.
- A journal: recovery keeps the run and stops, as [When recovery fails](#when-recovery-fails) describes for a malformed line, and the run is reconciled by the same steps.
- The lock's `info.json`: the lock cannot be judged stale without it, so it is refused as [The deployment lock](#the-deployment-lock) describes.
