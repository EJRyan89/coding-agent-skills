# Recovery

The deployer changes your home directory only through a journaled transaction, so an interrupted or failed deployment is put right by the next run. This page explains the state it keeps, what that next run does, and what to do in the few cases it stops and asks you to decide. Every deployer message that sends you here names the section to read. When a message alone does not show the cause, rerun the command with `--debug`, or with `DEPLOYER_DEBUG=1` set, to print the traceback after it.

## Where the deployer keeps its state

| Path | What it is |
| --- | --- |
| `~/.claude/skills/<name>` | A deployed skill or shared asset. |
| `~/.agents/skills/<name>` | A deployed runtime adapter for Codex and Copilot CLI. |
| `~/.claude/agents/<name>.md` | A deployed Claude Code subagent definition. |
| `~/.claude/skills/.deploy-manifest.json` | The ownership manifest: which source deployed each item, its hash, and the last committed run ID. |
| `~/.claude/deployer/config/<id>.config` | The configuration for one source, written by `python deploy.py configure`. |
| `~/.claude/deployer/staging/<run-id>/` | One run in progress: its rendered files and its journal, `journal.jsonl`. |
| `~/.claude/deployer/.deploy.lock.d/` | The lock held by the deployment that is running. |
| `<root>/<name>.deploying-bak` | The previous copy of an item while a run replaces or removes it. |
| `<root>/.backups/<run-id>/<name>` | A permanent backup of a copy a run replaced with `--force` or `--force-item`. |

`<root>` is whichever of `~/.claude/skills`, `~/.agents/skills`, and `~/.claude/agents` holds the item.

## Interrupted deployments

Before a run changes an item, it writes the change to its journal: a `backup` line when it moves the existing copy to `<name>.deploying-bak`, an `install` line when it moves the new copy into place, and a `preserve` line when it moves a backup to `.backups/<run-id>/`. Only after every item is in place does it record its run ID in the manifest, which commits the run. It then removes the transient backups it no longer needs, moves the ones it keeps to `.backups/`, and deletes its staging directory.

A run that fails with an error or is stopped with Ctrl+C reconciles its own journal before it exits. If it cannot, or the process is killed outright, for example by closing the terminal, its staging directory remains and the next run reconciles it before deploying anything:

- A run the manifest records as committed is completed: each installed item is checked against the hash in the journal, kept backups are moved to `.backups/<run-id>/`, and the other transient backups are deleted.
- A run that was not committed is rolled back: each item it installed is removed if it still matches what it installed, and each `<name>.deploying-bak` is moved back into place.

Either way, the staging directory is then deleted and the deployment you asked for continues. You do not need to do anything.

A dry run changes nothing, so it recovers nothing either. While an interrupted run is waiting, `--dry-run` says `Pending recovery: the next deployment will recover run <run-id>` and stops before planning, because recovery changes what is installed and a preview made before it would be wrong; leave the `.deploying-bak` files where they are. Deploy to recover and continue. If it instead says a run `cannot be recovered automatically`, the next deployment would stop too; see [When recovery fails](#when-recovery-fails).

## When recovery fails

Recovery stops instead of guessing when what it finds on disk does not match the journal: an item changed after the run installed it, a backup is missing, or an item and its `.deploying-bak` both exist. It then refuses to deploy, keeps the journal, and prints a `WARNING` line for each item it could not reconcile, followed by the run ID, whether that run was committed, and its staging directory. Other runs are still recovered.

A file recovery cannot read, move, or delete, usually because an editor or an antivirus scanner holds it open, stops it the same way, with `ERROR: Recovery of run <run-id> failed at <path>` and the reason. Close whatever holds that path and rerun; recovery repeats safely, picking up where it stopped. Reconcile by hand only if it fails again.

To reconcile a run by hand:

1. Do not rerun with `--force`; it would replace items whose correct state you have not checked yet.
2. Open the run's `journal.jsonl`. Each line names an `item` and a `root`: no `root` means `~/.claude/skills`, `agents` means `~/.agents/skills`, and `claude-agents` means `~/.claude/agents`. The items the `WARNING` lines name are the ones to look at.
3. If the run was committed, keep each item as it is. Move its `<name>.deploying-bak` to `.backups/<run-id>/<name>` in the same root if you want to keep the previous copy, or delete it once you have checked you do not need it.
4. If the run was not committed, put back what it replaced: move the current `<name>` out of the root, then rename `<name>.deploying-bak` to `<name>`. Moving it out of the root matters, because Claude Code, Codex, and Copilot would load anything left inside it.
5. When no `.deploying-bak` remains in any of the three roots, delete the run's staging directory, `~/.claude/deployer/staging/<run-id>/`.
6. Run `python deploy.py --all --dry-run`. An item whose content no longer matches the manifest is reported as modified and left alone; replace it with `--force-item <name>` when you are sure, which keeps a backup.

A journal line that cannot be parsed stops recovery the same way, with `Malformed journal entry`, and is reconciled by the same steps.

## Backups

A transient backup, `<name>.deploying-bak`, exists only while a run is replacing or removing that item, and recovery resolves it. One left behind with no staging directory to explain it makes the next deployment of that item refuse. Compare it with the item beside it, keep whichever copy you want, move the other out of the root, and rerun.

A permanent backup, `.backups/<run-id>/<name>`, is the copy a forced replacement overwrote. The deployer never deletes these. To restore one, move the current item out of the root and copy the backup into its place. The next deployment reports the restored item as modified and leaves it until you replace it with `--force-item <name>`. A run refuses to write a permanent backup over one that already exists; move the existing one out of the root after checking it, and rerun.

## The deployment lock

`~/.claude/deployer/.deploy.lock.d/` exists while a deployment runs, so two cannot run at once. Its `info.json` records the process ID and start time of the deployment that holds it. A lock whose process has exited, or whose process ID now belongs to another program, is stale, and the next run reclaims it with a warning. Reclaiming moves the lock aside to `~/.claude/deployer/.deploy.lock.stale.<pid>` and checks that what it moved is the lock it judged stale; if another deployment reclaimed it first and holds a fresh one, the run moves that lock back and stops with `contention`, so retry. In the rare case it cannot move it back, the message names the `.deploy.lock.stale.<pid>` directory; delete it once no deployment is running.

The deployer refuses only when it cannot tell: the lock has no readable `info.json`, or its process is running but cannot be confirmed to be the deployment that took the lock. Check that no `python deploy.py` process is running, for example with `Get-Process python` in PowerShell, then delete the lock directory and rerun. Removing it while a deployment is running would let a second one interleave with it.

## Ownership held by another source

Each repository you deploy from is a source with an ID in its `source.json`, and the manifest records which source deployed each item. A source refuses to replace an item another source owns, which keeps two clones from overwriting each other.

When the other source is an earlier ID of the same repository, or a clone you no longer deploy from, take its items over:

```bash
python deploy.py --migrate-from <source-id>
```

This changes nothing on disk. It moves ownership in the manifest of every item both sources deploy, provided each still matches the hash the old source recorded, and prints them under `MIGRATED`. Then deploy as usual. If an item is missing or has been changed since the old source deployed it, migration refuses without moving anything; restore that copy, for example by deploying from the old source again, and rerun it. `--migrate-from` cannot be combined with `--dry-run`, and the source IDs the manifest knows are listed under `sources` in `~/.claude/skills/.deploy-manifest.json`.

When both sources are still in use, do not migrate: the other source's next deployment would refuse in turn. Deploy the item from only one of them, by deselecting it in the other and redeploying that one.

A name one source deploys as a skill and another as a shared asset cannot be migrated, because it would have to be both. Rename it in one source, or stop deploying it from the other.

## Deploying from another checkout

The manifest also records the checkout each source was deployed from. Two clones of one repository share its source ID and its configuration, so a deployment from the second would silently take over every item the first deployed: it would point the installed skills, and `update-coding-agent-skills`, at the second clone and remove whatever the second lacks. A deployment from any checkout other than the recorded one therefore refuses before it changes anything, naming both paths.

If you deployed from the wrong clone, deploy from the recorded one instead. If this checkout replaces the recorded one, because you moved or re-cloned the repository, take the source over:

```bash
python deploy.py --all --take-over-source
```

It prints the recorded checkout and how many items the source owns, then deploys as usual; when the deployment commits, the manifest records this checkout, and later deployments from it need no flag. `--take-over-source` also works with `--migrate-from`. It cannot be combined with `--dry-run`, which never refuses, or with `--canary-home`, whose home starts empty. A linked worktree is refused whatever the flag; deploy from the main checkout.

## Configuration that no longer reads

`python deploy.py configure` writes each source's configuration, and every deployment reads it strictly. When an update removes a variable the file still sets, deployment stops, names the key, and asks you to run `python deploy.py configure`, which drops that key with a `NOTE`, keeps every other value, and saves the file. If the file no longer parses for another reason, for example because it was edited by hand, deployment stops and names the problem. `configure` reads the same file, so start from an empty one:

```bash
python deploy.py configure --reset
```

A directory value that no longer exists or is not absolute is rejected too; run `python deploy.py configure` and enter the new path.
