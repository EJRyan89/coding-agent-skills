"""The deployment pipeline: select, render, validate, and apply under a journal.

This module orchestrates a run: the lock, recovery, the journal, and the commit. The menu is in selection.py, the
per-item plan and its execution in plan.py, the reports in report.py, and --migrate-from in migrate.py.
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
import time
from pathlib import Path
from typing import TextIO

from . import (
    config,
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
)
from . import selection as selection_module
from .arguments import USAGE_ERROR, VERIFY_COMMAND_LINE, parse_command
from .context import Context, Options
from .errors import Cancelled, DeployError, debug_requested, fail, print_error, print_traceback, see_recovery
from .kinds import ADAPTER_KIND, AGENT_KIND, BY_LABEL, DEPENDENCY_KINDS, KINDS, SHARED, SKILL
from .paths import Paths, canary_home, claim_canary_home, validate_managed_roots
from .plan import INSTALLING, RELEASING, PlanEntry
from .report import DEPLOY_ACTIONS, DEPLOYED

TAKE_OVER_SOURCE = "--take-over-source"


def parse_arguments(namespace: argparse.Namespace, source_id: str) -> Options:
    """The deployment's options, refused when they combine in a way only the source can rule out or that cannot run."""
    options = Options(**{key: value for key, value in vars(namespace).items() if key != "command"})
    if options.include and not options.select_all:
        raise DeployError("ERROR: --include can only be used with --all")
    if options.migrate_from:
        if not source.SOURCE_ID_PATTERN.fullmatch(options.migrate_from):
            raise DeployError(f"ERROR: Invalid migration source ID: {options.migrate_from}")
        if options.migrate_from == source_id:
            raise DeployError("ERROR: --migrate-from must name a different source")
        if options.dry_run:
            raise DeployError("ERROR: --migrate-from cannot be combined with --dry-run")
    if options.take_over_source and options.dry_run:
        raise DeployError(f"ERROR: {TAKE_OVER_SOURCE} cannot be combined with --dry-run")
    if options.canary_home:
        for flag, used in (
            ("--dry-run", options.dry_run),
            ("--migrate-from", options.migrate_from),
            (TAKE_OVER_SOURCE, options.take_over_source),
        ):
            if used:
                raise DeployError(f"ERROR: --canary-home cannot be combined with {flag}")
    return options


def _apply(context: Context, entries: list[PlanEntry], staged: render.Staged, run_id: str) -> None:
    paths = context.paths
    # Read before anything moves, so git runs while no deployed item is half replaced.
    commit = source_commit.head_commit(paths.source_dir)
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
    _summary(context, run_id, entries, backups)


def _commit_manifest(context: Context, entries: list[PlanEntry], run_id: str, commit: str | None) -> None:
    owned, data = context.owned, context.manifest
    recorded: dict[str, dict[str, dict]] = {
        kind.key: {name: {"hash": value} for name, value in owned.of(kind).items()} for kind in KINDS
    }
    for name, owned_entry in recorded[SKILL.key].items():
        for dependency in DEPENDENCY_KINDS:
            owned_entry[dependency.dependency_key] = list(owned.deps_of(dependency).get(name, []))
    for name, owned_entry in recorded[SHARED.key].items():
        owned_entry["role"] = owned.shared_roles.get(name, "owner")
    for entry in entries:
        kind = BY_LABEL[entry.kind]
        if entry.action in RELEASING:
            recorded[kind.key].pop(entry.name, None)
        elif entry.action in INSTALLING:
            details: dict[str, object] = {"hash": entry.staged}
            if kind is SKILL:
                details[SHARED.dependency_key] = sorted(context.source.skills[entry.name].shared_deps)
                details[AGENT_KIND.dependency_key] = source.agent_closure(context.source, [entry.name])
            elif kind is SHARED:
                details["role"] = "owner"
            recorded[kind.key][entry.name] = details
    data.data["last_run_id"] = run_id
    # An entry written before selected_skills was dropped loses it here: nothing reads it.
    data.sources[context.source_id] = {
        "source_dir": platform_support.normalize(context.paths.source_dir),
        **({"source_commit": commit} if commit is not None else {}),
        "deployed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "requested_bundles": list(context.selection.bundles),
        "requested_skills": list(context.selection.skills),
        **{kind.key: dict(sorted(recorded[kind.key].items())) for kind in KINDS},
    }
    data.save()


def _finalize(context: Context, record: journal.Journal, run_id: str) -> list[tuple[str, str]]:
    """Move kept backups to permanent storage, delete the others, and return each (item, backup destination).

    A backup the run does not keep is deleted only while it matches the hash the plan recorded; one changed after the
    plan read it is kept like a modified item's, with a warning.
    """
    backups: list[tuple[str, str]] = []
    unkept: list[dict] = []
    for entry in list(record.entries):
        if entry["op"] != "backup":
            continue
        if not entry["retain"] and journal.backup_unchanged(context.paths, entry):
            unkept.append(entry)
            continue
        root = entry.get("root", "claude")
        item = entry["item"]
        transient = journal.root_directory(context.paths, root) / f"{item}.deploying-bak"
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
        fsops.remove(
            journal.root_directory(context.paths, entry.get("root", "claude")) / f"{entry['item']}.deploying-bak"
        )
    return backups


def _summary(context: Context, run_id: str, entries: list[PlanEntry], backups: list[tuple[str, str]]) -> None:
    report.print_report(DEPLOYED, DEPLOY_ACTIONS, [entry.report_line(DEPLOYED) for entry in entries])
    if backups:
        print(f"BACKED UP ({len(backups)}):")
        for item, destination in sorted(backups):
            print(f"  {item}: {destination}")
        print("")
    print(f"Run ID: {run_id}")
    print(f"Manifest: {platform_support.normalize(context.paths.manifest_file)}")
    print("")


def _deploy(
    paths: Paths, options: Options, src: source.Source, values: dict[str, str], stdin: TextIO, run_id: str | None
) -> int:
    data = manifest.load(paths.manifest_file)
    context = Context(paths, options, src, values, data, data.ownership(src.source_id), stdin)
    if options.migrate_from:
        migrate.migrate(context, options.migrate_from)
        return 0
    selection = selection_module.select(context)
    if selection is None:
        return 0
    context.selection = selection
    selected = [] if selection.deselect_all else source.expand(src, selection.bundles, selection.skills)
    adapters = [name for name in selected if src.skills[name].selectable]
    shadowed = [name for name in adapters if os.path.lexists(paths.copilot_skills_dir / name)]
    if shadowed:
        print("", file=sys.stderr)
        print(
            "WARNING: Higher-priority GitHub Copilot personal skills shadow generated runtime adapters:",
            file=sys.stderr,
        )
        for name in shadowed:
            print(f"  - {name} ({platform_support.normalize(paths.copilot_skills_dir / name)})", file=sys.stderr)
        print(
            "The deployer will not modify ~/.copilot/skills. Resolve these paths manually, then check "
            f"which copy Copilot uses with '{VERIFY_COMMAND_LINE}'.",
            file=sys.stderr,
        )
    selection_module.print_selection(src, selection, selected)
    selection_module.warn_missing_tools(src, selection)
    selection_module.require_variables(context, selected)
    staged_shared = plan.shared_to_stage(context, selected, plan.validate_shared_assets(context, selected))
    agents = source.agent_closure(src, selected)
    wanted = {SKILL: selected, SHARED: staged_shared, ADAPTER_KIND: adapters, AGENT_KIND: agents}
    plan.check_forced_items(context, wanted)
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
        return 0
    _apply(context, entries, staged, run_id)
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
    stdin = stdin if stdin is not None else sys.stdin
    debug = debug_requested(namespace.debug)
    try:
        paths, source_id, options, values, src = _prepare(namespace, paths)
    except (DeployError, OSError, KeyboardInterrupt) as exc:
        return fail(exc, debug, "prepare the deployment")
    _print_source(src, paths.home)
    if options.dry_run:
        return _dry_run(paths, options, src, values, stdin, debug)
    try:
        values, held = _claim(paths, options, source_id, values, probe)
    except (DeployError, OSError, KeyboardInterrupt) as exc:
        return fail(exc, debug, "prepare the deployment")
    try:
        _reject_other_checkout(paths, source_id, options.take_over_source)
    except (DeployError, OSError, KeyboardInterrupt) as exc:
        code = fail(exc, debug, "check the source checkout")
        held.release()
        return code
    try:
        recovered = journal.recover_incomplete(paths)
    except (Exception, KeyboardInterrupt) as exc:
        return _stopped_recovery(exc, debug, held)
    if not recovered:
        _report_failed_recovery()
        held.release()
        return 1
    # UTC, like deployed_at, so run IDs sort in the order the runs started, across time zones and daylight saving.
    run_id = f"{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}-{secrets.token_hex(2)}"
    try:
        code = _deploy(paths, options, src, values, stdin, run_id)
    except Cancelled as exc:
        # Raised only at the selection prompt, before staging or any destination change.
        print_error(exc, debug)
        held.release()
        return exc.exit_code
    except (Exception, KeyboardInterrupt) as exc:
        return _recover_from_failure(exc, debug, paths, held)
    held.release()
    return code


def _prepare(namespace: argparse.Namespace, paths: Paths) -> tuple[Paths, str, Options, dict[str, str], source.Source]:
    """The run's paths (a canary home's, when given), source ID, options, configured values, and source."""
    platform_support.ensure_supported()
    source_id = source.load_source_id(paths)
    options = parse_arguments(namespace, source_id)
    if options.canary_home:
        paths = Paths(paths.source_dir, canary_home(options.canary_home))
    validate_managed_roots(paths)
    # A canary home's values are set when it is claimed, after every check; the real configuration is unread.
    values = (
        {}
        if options.canary_home
        else config.load(paths.config_file(source_id), source_id, paths.home, paths.source_dir)
    )
    return paths, source_id, options, values, source.discover(paths, source_id)


def _dry_run(
    paths: Paths, options: Options, src: source.Source, values: dict[str, str], stdin: TextIO, debug: bool
) -> int:
    """Report what a deployment would do, holding no lock and changing nothing, unless a recovery is pending."""
    try:
        if _stop_for_pending_recovery(paths):
            return 0
        return _deploy(paths, options, src, values, stdin, None)
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
    print("", file=sys.stderr)
    print("ERROR: Recovery failed, so nothing was deployed.", file=sys.stderr)
    print(
        f"Reconcile the run named above by hand, then rerun with --dry-run. {see_recovery(RECOVERY_FAILED)}",
        file=sys.stderr,
    )
    print("", file=sys.stderr)


def _stopped_recovery(exc: BaseException, debug: bool, held: lock.Lock) -> int:
    """Report recovery stopped by Ctrl+C or an unexpected error, then release the lock.

    Recovery replays its journal, which stays on disk until a run is reconciled, so the next deployment finishes it.
    """
    if isinstance(exc, KeyboardInterrupt):
        error = DeployError(RECOVERY_CANCELLED, exit_code=130)
    else:
        error = DeployError(
            f"ERROR: Unexpected {type(exc).__name__}: {exc}",
            "Recovery stopped, so nothing was deployed; the next deployment retries it.",
            see_recovery(RECOVERY_FAILED),
        )
    error.__cause__ = exc
    print_error(error, debug)
    held.release()
    return error.exit_code


def _recover_from_failure(exc: BaseException, debug: bool, paths: Paths, held: lock.Lock) -> int:
    """Report a failed deployment, then recover its journal and release the lock, or keep both when that fails."""
    if isinstance(exc, DeployError):
        print_error(exc, debug)
        code = exc.exit_code
    else:
        print(f"ERROR: Unexpected {type(exc).__name__}: {exc}", file=sys.stderr)
        if debug:
            print_traceback(exc)
        code = 130 if isinstance(exc, KeyboardInterrupt) else 1
    print("Deployment failed; reconciling the current journal before exit...", file=sys.stderr)
    if journal.recover_incomplete(paths):
        held.release()
    else:
        print("ERROR: Immediate recovery failed; deployment evidence and lock were retained.", file=sys.stderr)
        print(
            "The next run reclaims the lock and retries recovery. If that fails too, "
            f"reconcile the run by hand. {see_recovery(RECOVERY_FAILED)}",
            file=sys.stderr,
        )
    print("", file=sys.stderr)
    return code
