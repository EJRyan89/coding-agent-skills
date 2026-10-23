"""The deployment pipeline: select, render, validate, and apply under a journal.

This module orchestrates a run: the lock, recovery, the journal, and the commit. The menu is in selection.py, the
per-item plan and its execution in plan.py, the reports in report.py, and --migrate-from in migrate.py.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import secrets
import sys
import time
from pathlib import Path
from typing import TextIO, assert_never

from . import (
    config,
    errors,
    fsops,
    journal,
    lock,
    manifest,
    migrate,
    plan,
    platform_support,
    render,
    report,
    source,
    source_commit,
    source_ref,
)
from . import selection as selection_module
from .arguments import PROG, USAGE_ERROR, parse_command, usage_error
from .context import Context, Options
from .errors import Cancelled, DeployError, debug_requested, fail, print_error, print_traceback, see_recovery
from .kinds import ADAPTER_KIND, AGENT_KIND, DEPENDENCY_KINDS, KINDS, SHARED, SKILL, KindKey
from .manifest import ItemEntry, source_entry
from .names import transient_name
from .paths import Paths, canary_home, claim_canary_home, validate_managed_roots
from .plan import PlanEntry
from .report import DEPLOY_ACTIONS, DEPLOYED

KINDS_ALLOWED = {
    "_commit_manifest": "a skill entry also records the items it depends on, and a shared asset entry its role",
    "_deploy": "each kind's wanted items come from their own step (the selected skills, the shared assets staged for "
    "them, their runtime adapters, and the agents they need), and plan.build plans each kind its own way",
}

TAKE_OVER_SOURCE = "--take-over-source"


def parse_arguments(namespace: argparse.Namespace, source_id: str) -> Options:
    """The deployment's options, with --migrate-from's source ID refused unless it names another valid source.

    The parser refuses options that cannot run together; this checks what only the source can rule out.
    """
    options = Options(**{key: value for key, value in vars(namespace).items() if key != "command"})
    if options.migrate_from:
        if not source.SOURCE_ID_PATTERN.fullmatch(options.migrate_from):
            raise usage_error(f"Invalid migration source ID: {options.migrate_from}")
        if options.migrate_from == source_id:
            raise usage_error("--migrate-from must name a different source")
    return options


def _apply(
    context: Context, entries: list[PlanEntry], staged: render.Staged, run_id: str, replaced_pin: str | None
) -> None:
    paths = context.paths
    # Read before anything moves, so git runs while no deployed item is half replaced.
    commit = context.pin.commit if context.pin is not None else source_commit.head_commit(paths.source_dir)
    staging_dir = paths.staging_root / run_id
    staged.write(staging_dir)
    record = journal.Journal(staging_dir / "journal.jsonl", run_id)
    record.create()
    for root in dict.fromkeys(kind.root for kind in KINDS):
        fsops.make_directories(journal.root_directory(paths, root))
    for entry in entries:
        plan.carry_out(record, paths, staging_dir, entry)
    _commit_manifest(context, entries, run_id, commit)
    backups = _finalize(context, record, run_id)
    fsops.remove(staging_dir)
    _summary(context, run_id, entries, backups, replaced_pin)


def _commit_manifest(context: Context, entries: list[PlanEntry], run_id: str, commit: str | None) -> None:
    owned, data = context.owned, context.manifest
    recorded: dict[KindKey, dict[str, ItemEntry]] = {
        kind.key: {name: {"hash": value} for name, value in owned.of(kind).items()} for kind in KINDS
    }
    for name, owned_entry in recorded[SKILL.key].items():
        for dependency in DEPENDENCY_KINDS:
            owned_entry[dependency.dependency_key] = list(owned.deps_of(dependency).get(name, []))
    for name, owned_entry in recorded[SHARED.key].items():
        owned_entry["role"] = owned.shared_roles.get(name, "owner")
    for entry in entries:
        kind = entry.kind
        match entry.action:
            case "REMOVE" | "DROP":  # the action ends this source's ownership of the item
                recorded[kind.key].pop(entry.name, None)
            case "INSTALL" | "UPDATE" | "UNCHANGED" | "ADOPT" | "REPLACE" | "RENAME":  # it records the rendered copy
                # A rename's rendered copy takes the place of the old name's, whose ownership ends.
                recorded[kind.key].pop(entry.previous, None)
                details: ItemEntry = {"hash": entry.staged}
                if kind is SKILL:
                    details[SKILL.dependency_key] = sorted(context.source.skills[entry.name].skill_deps)
                    details[SHARED.dependency_key] = sorted(context.source.skills[entry.name].shared_deps)
                    details[AGENT_KIND.dependency_key] = source.agent_closure(context.source, [entry.name])
                elif kind is SHARED:
                    details["role"] = "owner"
                recorded[kind.key][entry.name] = details
            case "SKIP" | "PRESERVE" | "KEEP":  # the item keeps what the manifest recorded for it
                pass
            case _:
                assert_never(entry.action)
    data.data["last_run_id"] = run_id
    pinned = (data.source_ref(context.source_id), data.source_commit(context.source_id))
    # An entry written before selected_skills was dropped loses it here: nothing reads it.
    data.sources[context.source_id] = source_entry(
        platform_support.normalize(context.paths.source_dir),
        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        commit=commit,
        ref=source_ref.recorded_ref(context.pin, *pinned, unpin=context.options.unpin),
        requested_bundles=context.selection.bundles,
        requested_bundle_members={name: sorted(context.source.bundles[name]) for name in context.selection.bundles},
        requested_skills=context.selection.skills,
        items=recorded,
    )
    data.save()


def _finalize(context: Context, record: journal.Journal, run_id: str) -> list[tuple[str, str]]:
    """Move kept backups to permanent storage, delete the others, and return each (item, backup destination).

    A backup the run does not keep is deleted only while it matches the hash the plan recorded; one changed after the
    plan read it is kept like a modified item's, with a warning.
    """
    backups: list[tuple[str, str]] = []
    unkept: list[journal.BackupEntry] = []
    for entry in list(record.entries):
        if entry["op"] != "backup":
            continue
        if not entry["retain"] and journal.backup_unchanged(context.paths, entry):
            unkept.append(entry)
            continue
        root = journal.entry_root(entry)
        item = entry["item"]
        transient = journal.root_directory(context.paths, root) / transient_name(item)
        try:
            destination = journal.prepare_backup_destination(context.paths, run_id, item, root)
        except DeployError as exc:
            raise DeployError(*exc.lines, f"ERROR: Refusing unsafe permanent backup destination for {item}") from exc
        if os.path.lexists(transient):
            if os.path.lexists(destination):
                raise DeployError(
                    f"ERROR: Permanent backup destination already exists: {platform_support.normalize(destination)}",
                    "Move it out of the deployment root after checking its contents, then retry.",
                    see_recovery("Backups"),
                )
            record.preserve(root, item)
            fsops.move(transient, destination)
            kept_at = f".backups/{run_id}/{item}"
            backups.append((item, kept_at))
            if not entry["retain"]:
                print(
                    f"WARNING: {item} changed after this deployment planned it, so its previous copy was kept at "
                    f"{kept_at} instead of being deleted.",
                    file=sys.stderr,
                )
    for entry in unkept:
        fsops.remove(journal.root_directory(context.paths, journal.entry_root(entry)) / transient_name(entry["item"]))
    return backups


def _summary(
    context: Context,
    run_id: str,
    entries: list[PlanEntry],
    backups: list[tuple[str, str]],
    replaced_pin: str | None,
) -> None:
    report.print_report(DEPLOYED, DEPLOY_ACTIONS, [entry.report_line(DEPLOYED) for entry in entries])
    if backups:
        print(f"BACKED UP ({len(backups)}):")
        for item, destination in sorted(backups):
            print(f"  {item}: {destination}")
        print("")
    if replaced_pin is not None:
        print(f"Replaced {replaced_pin}.")
        print("")
    print(f"Run ID: {run_id}")
    print(f"Manifest: {platform_support.normalize(context.paths.manifest_file)}")
    print("")


def _replaced_pin(context: Context) -> str | None:
    """The deployment made with --at that this one replaces, described, or None when it replaces none.

    A deployment pinned to the same commit keeps the pin and its recorded ref (source_ref.recorded_ref); any other
    moves the installation off it, which update-coding-agent-skills reports from the line this describes.
    """
    ref = context.manifest.source_ref(context.source_id)
    commit = context.manifest.source_commit(context.source_id)
    if ref is None or (context.pin is not None and context.pin.commit == commit):
        return None
    return f"the deployment pinned with --at {ref} to {source_commit.describe(context.paths.source_dir, commit)}"


def _refuse_unpinning(context: Context, replaced_pin: str | None) -> None:
    """Refuse a run that would replace a deployment made with --at, unless it passes --unpin or uninstalls.

    The deployer runs from the checkout's HEAD, so this holds a pin whichever update-coding-agent-skills started the
    run, even one that runs `--all`, or `--all --at refs/tags/<tag>`, over a pin it could not read; another --at may
    move only that skill's own pin at a release (source_ref.is_release_pin).
    """
    if replaced_pin is None or context.options.unpin or context.selection.deselect_all:
        return
    recorded = (context.manifest.source_ref(context.source_id), context.manifest.source_commit(context.source_id))
    if context.pin is not None and source_ref.is_release_pin(context.paths.source_dir, *recorded):
        return
    # A run at any other commit, with --at or without, is refused here alike, so the pin is kept by changing nothing.
    move, rerun = (
        ("this checkout's HEAD", f"{PROG} --all --unpin")
        if context.pin is None
        else (context.pin.ref, f"{PROG} --all --at {context.pin.ref} --unpin")
    )
    raise DeployError(
        f"ERROR: Refusing to replace {replaced_pin} without --unpin.",
        f"To move it to {move}, rerun with --unpin, as in '{rerun}'; to keep the pin, change nothing.",
        see_recovery(source_ref.EARLIER_COMMIT),
    )


def _deploy(
    paths: Paths,
    options: Options,
    src: source.Source,
    values: dict[str, str],
    stdin: TextIO,
    run_id: str | None,
    pin: source_ref.Pin | None = None,
) -> int:
    data = manifest.load(paths.manifest_file)
    context = Context(paths, options, src, values, data, data.ownership(src.source_id), stdin, pin=pin)
    if options.migrate_from:
        migrate.migrate(context, options.migrate_from)
        return 0
    selection = selection_module.select(context)
    if selection is None:
        return 0
    context.selection = selection
    replaced_pin = _replaced_pin(context)
    _refuse_unpinning(context, replaced_pin)
    selected = [] if selection.deselect_all else source.expand(src, selection.bundles, selection.skills)
    adapters = [name for name in selected if src.skills[name].selectable]
    report.warn_copilot(paths, adapters)
    selection_module.print_selection(src, selection, selected)
    selection_module.warn_missing_tools(src, selection)
    selection_module.require_variables(context, selected)
    staged_shared = plan.shared_to_stage(context, selected, plan.validate_shared_assets(context, selected))
    agents = source.agent_closure(src, selected)
    wanted = {SKILL: selected, SHARED: staged_shared, ADAPTER_KIND: adapters, AGENT_KIND: agents}
    plan.check_forced_items(options.force_items, wanted)
    plan.check_ownership(context, wanted)
    staged = render.render(src, selected, adapters, staged_shared, values, paths.skills_src, agents)
    render.reject_unexpanded_tokens(staged)
    render.validate_executables(staged)
    print("")
    if run_id is None:
        print("Rendered and validated in memory (dry run).")
    else:
        print("Rendered and validated.")
    # Planned last, after the slow validation, so the destinations it read are the ones the deployment changes.
    entries = plan.build(context, wanted, staged)
    if run_id is None:
        plan.dry_run(entries)
        if replaced_pin is not None:
            print(f"Would replace {replaced_pin}.")
            print("")
        return 0
    _apply(context, entries, staged, run_id, replaced_pin)
    return 0


def _print_source(src: source.Source, home: Path) -> None:
    print("")
    print(f"Source: {source.label(src.source_id, src.name)}")
    print(f"Home: {platform_support.normalize(home)}")
    found = report.plural(len(src.skills), "skill")
    if src.bundles:
        found += f" and {report.plural(len(src.bundles), 'bundle')}"
    print(f"Found {found}.")
    report.warn_ignored_home(home)


RECOVERY_FAILED = "When recovery fails"
RECOVERY_CANCELLED = "Cancelled during recovery; the next deployment finishes recovering before it deploys."


def _stop_for_pending_recovery(paths: Paths) -> bool:
    """Say which interrupted runs the next deployment will reconcile, and stop a dry run when there are any.

    Recovery changes what is installed, so a preview planned before it would describe the wrong tree, and its leftover
    .deploying-bak files would read as stale backups to delete by hand.
    """
    pending = journal.pending_runs(paths)
    if not pending:
        return False
    stuck = [run_id for run_id, recoverable in pending if not recoverable]
    lines = [
        *(
            f"Pending recovery: the next deployment will recover run {run_id}."
            for run_id, recoverable in pending
            if recoverable
        ),
        *(
            f"ERROR: Run {run_id} cannot be recovered automatically; the next deployment will stop until it is "
            "reconciled by hand."
            for run_id in stuck
        ),
        "The dry run stops here: recovery changes what is installed, so a preview before it would be wrong.",
        see_recovery(RECOVERY_FAILED if stuck else "Interrupted deployments"),
    ]
    if stuck:
        raise DeployError(*lines)
    print("")
    print(*lines, sep="\n")
    print("")
    return True


def _reject_other_checkout(paths: Paths, source_id: str, take_over: bool) -> None:
    """Refuse a checkout other than the one the manifest records for this source, unless the user takes it over.

    Two clones share a source ID and a configuration, so a deployment from the wrong one would silently re-point every
    installed skill at it and remove whatever it lacks.
    """
    entry = manifest.load(paths.manifest_file).source(source_id) or {}
    recorded = entry.get("source_dir")
    if not isinstance(recorded, str) or not recorded:
        return
    if platform_support.same_directory(Path(recorded), paths.source_dir):
        return
    current = platform_support.normalize(paths.source_dir)
    if not take_over:
        gone = "" if Path(recorded).is_dir() else ", which no longer exists"
        raise DeployError(
            f"ERROR: Source '{source_id}' is deployed from {recorded}{gone}, not from this checkout, {current}.",
            f"Deploy from {recorded}; or, if this checkout replaces it, rerun with {TAKE_OVER_SOURCE} to record this "
            "checkout as its source.",
            see_recovery("Deploying from another checkout"),
        )
    owned = ", ".join(report.plural(len(entry.get(kind.key) or {}), kind.noun) for kind in KINDS)
    print("")
    print(f"Taking over source '{source_id}' from {recorded}: {owned}.")
    print(f"This checkout, {current}, is recorded as their source when this deployment commits.")


def run(
    arguments: list[str],
    paths: Paths,
    *,
    probe: lock.ProcessProbe = platform_support.process_status,
    stdin: TextIO | None = None,
) -> int:
    """Deploy with deploy.py's options, as `python deploy.py` with no subcommand does."""
    namespace = parse_command(arguments)
    if isinstance(namespace, int):
        return namespace
    if namespace.command is not None:
        print_error(DeployError(f"ERROR: '{namespace.command}' is a command; run it with deploy.py itself."))
        return USAGE_ERROR
    return execute(namespace, paths, probe=probe, stdin=stdin)


def execute(
    namespace: argparse.Namespace,
    paths: Paths,
    *,
    probe: lock.ProcessProbe = platform_support.process_status,
    stdin: TextIO | None = None,
) -> int:
    """Deploy the checkout, or, with --at, the tree of the commit it names, exported for the run and removed after."""
    stdin = stdin if stdin is not None else sys.stdin
    debug = debug_requested(namespace.debug)
    if namespace.at is None:
        return _execute(namespace, paths, None, probe, stdin, debug)
    try:
        platform_support.ensure_supported()
        source_ref.require_extraction_filters()
        pin = source_ref.resolve(paths.source_dir, namespace.at)
        exported = source_ref.export(paths.source_dir, pin.commit)
    except (DeployError, OSError, KeyboardInterrupt) as exc:
        return fail(exc, debug, "export the commit --at names")
    try:
        return _execute(namespace, dataclasses.replace(paths, tree_dir=exported / "tree"), pin, probe, stdin, debug)
    finally:
        source_ref.remove(exported)


def _execute(
    namespace: argparse.Namespace,
    paths: Paths,
    pin: source_ref.Pin | None,
    probe: lock.ProcessProbe,
    stdin: TextIO,
    debug: bool,
) -> int:
    try:
        paths, source_id, options, values, src = _prepare(namespace, paths)
    except (DeployError, OSError, KeyboardInterrupt) as exc:
        return fail(exc, debug, "prepare the deployment")
    _print_source(src, paths.home)
    if pin is not None:
        print(f"At: {pin.ref} ({pin.commit})")
    if options.dry_run:
        return _dry_run(paths, options, src, values, stdin, debug, pin)
    try:
        values, held = _claim(paths, options, source_id, values, probe)
    except (DeployError, OSError, KeyboardInterrupt) as exc:
        return fail(exc, debug, "prepare the deployment")
    # The one release point: every way out of this block releases the lock unless a failed recovery retained it.
    with held:
        try:
            _reject_other_checkout(paths, source_id, options.take_over_source)
        except (DeployError, OSError, KeyboardInterrupt) as exc:
            return fail(exc, debug, "check the source checkout")
        try:
            recovered = journal.recover_incomplete(paths)
        except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - any failure while recovering ends in the one stopped-recovery report
            return _stopped_recovery(exc, debug, paths)
        if not recovered:
            _report_failed_recovery()
            return 1
        # UTC, like deployed_at, so run IDs sort in the order the runs started, across time zones and daylight saving.
        run_id = f"{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}-{secrets.token_hex(2)}"
        try:
            return _deploy(paths, options, src, values, stdin, run_id, pin)
        except DeployError as exc:
            if not isinstance(exc, Cancelled) and exc.exit_code != USAGE_ERROR:
                return _recover_from_failure(exc, debug, paths, held)
            # Selection cancellation and usage checks happen before staging or any destination change.
            print_error(exc, debug)
            return exc.exit_code
        except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - any failure after the lock is taken is recovered from before the run ends
            return _recover_from_failure(exc, debug, paths, held)


def _prepare(namespace: argparse.Namespace, paths: Paths) -> tuple[Paths, str, Options, dict[str, str], source.Source]:
    """The run's paths (a canary home's, when given), source ID, options, configured values, and source."""
    platform_support.ensure_supported()
    source_id = source.load_source_id(paths)
    if paths.tree_dir is not None:
        _reject_other_source_id(paths, source_id)
    options = parse_arguments(namespace, source_id)
    if options.canary_home:
        # The canary's runtimes read their configuration from the canary home, never from the user's COPILOT_HOME.
        paths = dataclasses.replace(paths, home=canary_home(options.canary_home), copilot_home=None)
    validate_managed_roots(paths)
    # A canary home's values are set when it is claimed, after every check; the real configuration is unread.
    values = (
        {}
        if options.canary_home
        else config.load(paths.config_file(source_id), source_id, paths.home, paths.source_dir)
    )
    src = source.discover(paths, source_id)
    selection_module.check_includes(src, options.include)
    plan.check_source_forced_items(src, options.force_items)
    return paths, source_id, options, values, src


def _reject_other_source_id(paths: Paths, tree_id: str) -> None:
    """Refuse an exported tree whose source.json names another source than the checkout's.

    Deploying it would move every item to another owner, which only an explicit --migrate-from may do.
    """
    checkout_id = source.load_source_id(dataclasses.replace(paths, tree_dir=None))
    if tree_id != checkout_id:
        raise DeployError(
            f"ERROR: The commit --at names has source ID '{tree_id}' in its source.json, not this checkout's "
            f"'{checkout_id}'.",
            "Name a commit of this source; a change of source ID needs an explicit ownership migration.",
            see_recovery(source_ref.EARLIER_COMMIT),
        )


def _dry_run(
    paths: Paths,
    options: Options,
    src: source.Source,
    values: dict[str, str],
    stdin: TextIO,
    debug: bool,
    pin: source_ref.Pin | None = None,
) -> int:
    """Report what a deployment would do, holding no lock and changing nothing, unless a recovery is pending."""
    try:
        if _stop_for_pending_recovery(paths):
            return 0
        return _deploy(paths, options, src, values, stdin, None, pin)
    except (DeployError, OSError, KeyboardInterrupt) as exc:
        return fail(exc, debug, "finish the dry run")


def _claim(
    paths: Paths, options: Options, source_id: str, values: dict[str, str], probe: lock.ProcessProbe
) -> tuple[dict[str, str], lock.Lock]:
    """Claim a canary home and its values, or refuse a linked worktree; then take the lock."""
    if options.canary_home:
        # A throwaway home is discarded with its recorded source path, so a linked worktree may deploy into it.
        claim_canary_home(paths.home)
        values = config.canary(source_id, paths.home, paths.source_dir)
    else:
        source.reject_linked_worktree(paths)
    return values, lock.acquire(paths, probe)


def _report_failed_recovery() -> None:
    # Whether each run was committed is printed above, with the run, since an earlier run's changes may be installed.
    print("", file=sys.stderr)
    print("ERROR: Recovery failed, so this deployment stopped before deploying anything.", file=sys.stderr)
    print(
        f"Reconcile the run named above by hand, then rerun with --dry-run. {see_recovery(RECOVERY_FAILED)}",
        file=sys.stderr,
    )
    print("", file=sys.stderr)


def _run_states(paths: Paths) -> list[str]:
    """Say, for each run whose journal remains, whether the manifest holds its changes or the retry rolls them back.

    A run committed before recovery stopped is installed, so the report must not say that nothing was deployed.
    """
    return [
        f"Run {run_id} was committed to the manifest, so its changes are installed and the manifest holds them; "
        "the next deployment finishes its cleanup."
        if committed
        else f"Run {run_id} was not committed to the manifest, so the manifest does not hold its changes; "
        "the next deployment rolls them back."
        for run_id, committed in journal.journaled_runs(paths) or []
    ]


def _stopped_recovery(exc: BaseException, debug: bool, paths: Paths) -> int:
    """Report recovery stopped by Ctrl+C or an unexpected error; the caller releases the lock.

    Recovery replays its journal, which stays on disk until a run is reconciled, so the next deployment finishes it.
    """
    if isinstance(exc, KeyboardInterrupt):
        error = DeployError(RECOVERY_CANCELLED, *_run_states(paths), exit_code=130)
    else:
        error = DeployError(
            errors.named(exc, "finish recovery"),
            "Recovery stopped before it finished; the next deployment retries it before deploying anything.",
            *_run_states(paths),
            see_recovery(RECOVERY_FAILED),
        )
    error.__cause__ = exc
    print_error(error, debug)
    return error.exit_code


def _recover_from_failure(exc: BaseException, debug: bool, paths: Paths, held: lock.Lock) -> int:
    """Report a failed deployment, then recover its journal.

    An exception during recovery keeps the journal; unreconciled entries keep both it and the lock, which this
    retains so the caller's release leaves it.
    """
    outcome = "cancelled" if isinstance(exc, KeyboardInterrupt) else "failed"
    # An OSError, such as of a file another program holds open, ends as every command's does; Ctrl+C as a cancellation.
    if isinstance(exc, (OSError, KeyboardInterrupt)):
        exc = errors.converted(exc, "finish the deployment", errors.DEPLOYMENT_CANCELLED)
    if isinstance(exc, DeployError):
        print_error(exc, debug)
        code = exc.exit_code
    else:
        print(errors.named(exc, "finish the deployment"), file=sys.stderr)
        if debug:
            print_traceback(exc)
        code = 1
    print(f"Deployment {outcome}; reconciling the current journal before exit...", file=sys.stderr)
    try:
        recovered = journal.recover_incomplete(paths)
    except (Exception, KeyboardInterrupt) as recovery_error:  # noqa: BLE001 - any failure while recovering ends in the one stopped-recovery report
        return _stopped_recovery(recovery_error, debug, paths)
    if not recovered:
        held.retain()
        print("ERROR: Immediate recovery failed; deployment evidence and lock were retained.", file=sys.stderr)
        print(
            "The next run reclaims the lock and retries recovery. If that fails too, "
            f"reconcile the run by hand. {see_recovery(RECOVERY_FAILED)}",
            file=sys.stderr,
        )
    print("", file=sys.stderr)
    return code
