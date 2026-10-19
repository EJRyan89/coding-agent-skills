"""A run's reviewer: materializing the generic or a repository reviewer, and inspecting and validating one."""

from __future__ import annotations

import functools
import re
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

from review_config import default_config_path, load_config, validate_repository_identity
from review_guard import RUN_PREFIX
from review_io import atomic_write_text
from review_operation import validate_canary_pull
from review_records import RE_REVIEW_SCOPES
from review_reviewers import (
    MissingSkillError,
    ResolvedReviewer,
    inspect_configured_skill,
    manifest_location,
    repository_files,
    resolve_reviewer,
)
from review_run import PipelineError, Services
from review_runtime import (
    MAX_SOURCE_SNAPSHOT_BYTES,
    PULL_REQUEST_BODY_CHARACTERS,
    RuntimeContractError,
    choose_dispatch,
    declared_reviewer_files,
    git_in,
    glob_matcher,
    materialize_reviewer,
    materialize_source_snapshot,
    measure_source_snapshot,
    negotiate_capabilities,
    resolve_reviewer_commit,
    verify_checkout_remote,
)
from review_snapshot import _route_reads, describe_fallback, ensure_local_commit, lazy_upfront, read_pull_diff
from review_specialists import (
    condition_reads,
    evaluate_condition,
    parse_unified_diff,
    renamed_from,
    route,
    specialist_model,
    uncovered,
    unreviewed,
)
from review_vocabularies import DispatchMode


def _generic_reviewer(runtime: str, inline: bool, source: str) -> tuple[str, dict[str, Any], None, None, DispatchMode]:
    """The suite's generic reviewer, as `_materialize_reviewer` returns it: `source` is `generic` when the repository
    is configured with it, and `generic-fallback` when it reviews in place of a repository reviewer."""
    dispatch = choose_dispatch(runtime, "generic", inline=inline)
    # The suite's own role needs agent delegation only when a subagent works it.
    negotiate_capabilities(runtime, ["agent-delegation"] if dispatch == "subagents" else [], dispatch=dispatch)
    adapter: dict[str, Any] = {
        "name": "generic",
        "scope": "generic",
        "source": source,
        "source_commit": None,
        "source_hashes": {},
    }
    return "generic", adapter, None, None, dispatch


def _default_branch_tip(checkout: Path, services: Services) -> tuple[str, str]:
    """origin's default branch and the commit at its tip, as origin reports them now.

    The checkout's origin is the configured repository, so its default branch holds only what the repository merged.
    """
    result = git_in(checkout, services.git, "ls-remote", "--symref", "origin", "HEAD")
    if result.returncode != 0:
        reason = result.stderr.strip() or "git ls-remote failed"
        raise PipelineError(f"Cannot read origin's default branch: {reason}")
    branch, tip = "", ""
    for line in result.stdout.splitlines():
        value, _, ref = line.partition("\t")
        if ref != "HEAD":
            continue
        if value.startswith("ref: refs/heads/"):
            branch = value.removeprefix("ref: refs/heads/")
        elif re.fullmatch(r"[0-9a-fA-F]{40,64}", value):
            tip = value.lower()
    if not tip:
        raise PipelineError("Cannot read origin's default branch: origin names no default branch")
    return branch, tip


def _resolve_reviewer_source(
    reviewer: dict[str, Any],
    *,
    checkout: Path,
    pull: dict[str, Any],
    config_path: Path,
    repository: str,
    services: Services,
    notes: list[str],
) -> tuple[str, str, ResolvedReviewer] | None:
    """Where a repository reviewer is read from: `trusted-ref` or `base`, the commit, and what it resolved to there.

    A configured skill the base predates, with no trusted ref, is read from the default branch's tip instead
    (`default-branch`). None, with a note, when the tip lacks it too or is the pull request's head: the suite's
    generic reviewer then reviews the pull request, since the head never supplies its own reviewer.
    """
    head, base = pull["headRefOid"].lower(), pull["baseRefOid"]
    source = "trusted-ref" if reviewer["trusted_ref"] else "base"
    commit = resolve_reviewer_commit(checkout, reviewer["trusted_ref"] or base, head_sha=head, runner=services.git)
    resolve = functools.partial(
        resolve_reviewer,
        reviewer,
        checkout=checkout,
        config_path=config_path,
        repository=repository,
        runner=services.git,
    )
    try:
        return source, commit, resolve(commit=commit)
    except MissingSkillError:
        if source != "base":
            raise
    skill = reviewer["skill"]
    name, tip = _default_branch_tip(checkout, services)
    branch = name or "origin's default branch"
    if tip == head:
        notes.append(
            f"The base {commit[:12]} predates the review skill {skill}, and {branch}'s tip is this pull request's "
            "head, which never supplies its own reviewer, so the suite's generic reviewer reviews this pull request."
        )
        return None
    if tip != commit:
        ensure_local_commit(checkout, tip, f"refs/heads/{name}" if name else "HEAD", services.git)
        try:
            resolved = resolve(commit=tip)
        except MissingSkillError:
            pass
        else:
            notes.append(
                f"The base {commit[:12]} predates the review skill {skill}, so the reviewer was read from {branch}'s "
                f"tip {tip[:12]}."
            )
            return "default-branch", tip, resolved
    notes.append(
        f"Neither the base {commit[:12]} nor {branch}'s tip {tip[:12]} has the review skill {skill}, so the suite's "
        "generic reviewer reviews this pull request."
    )
    return None


def _materialize_reviewer(
    reviewer: dict[str, Any],
    *,
    checkout: Path | None,
    pull: dict[str, Any],
    mode: str,
    runtime: str,
    inline: bool,
    run: Path,
    config_path: Path,
    repository: str,
    services: Services,
    notes: list[str],
) -> tuple[str, dict[str, Any], Path | None, str | None, DispatchMode]:
    """The reviewer's kind, the adapter the record names, the root its trusted files were materialized under, its
    entrypoint, and how its roles are dispatched. The suite's generic reviewer has no root and no entrypoint, and
    neither has a specialists manifest."""
    if reviewer["scope"] == "generic":
        return _generic_reviewer(runtime, inline, "generic")
    if checkout is None:  # validate_config requires a checkout for a repository reviewer
        raise PipelineError(f"{repository} has a repository reviewer but no checkout_path")
    ensure_local_commit(checkout, pull["baseRefOid"], f"refs/heads/{pull['baseRefName']}", services.git)
    found = _resolve_reviewer_source(
        reviewer,
        checkout=checkout,
        pull=pull,
        config_path=config_path,
        repository=repository,
        services=services,
        notes=notes,
    )
    if found is None:
        return _generic_reviewer(runtime, inline, "generic-fallback")
    source, reviewer_commit, resolved = found
    manifest = resolved.manifest
    if resolved.inspection is not None and resolved.source == "skill" and resolved.inspection.delegates == "unknown":
        notes.append(
            f"{resolved.inspection.skill} may start subagents ({resolved.inspection.reason}); "
            "if its review fails, give it a specialists manifest."
        )
    if mode not in manifest["supports"]:
        raise PipelineError(f"Reviewer {manifest['id']} does not support {mode} reviews")
    kind = "specialists" if manifest.get("kind") == "specialists" else "entrypoint"
    dispatch = choose_dispatch(runtime, kind, inline=inline)
    # A specialists manifest that lists agent-delegation keeps its specialists off an inline review.
    negotiate_capabilities(runtime, manifest["required_capabilities"], dispatch=dispatch)
    reviewer_root = run / "reviewer"
    hashes = materialize_reviewer(
        checkout,
        reviewer_commit,
        manifest,
        reviewer_root,
        runner=services.git,
        guideline_commit=pull["baseRefOid"],
        local_root=resolved.local_root,
    )
    adapter = {
        "name": manifest["id"],
        "scope": "repository",
        "source": source,
        "source_commit": reviewer_commit,
        "source_hashes": hashes,
    }
    return kind, adapter, reviewer_root, manifest["entrypoint"] if kind == "entrypoint" else None, dispatch


# The reviewer of a fixture whose pull.json names no reviewer manifest.
GENERIC_REVIEWER = {
    "id": "generic",
    "protocol_version": 1,
    "trusted_ref": None,
    "scope": "generic",
    "manifest_path": None,
}


def fixture_reviewer(manifest_path: str) -> dict[str, Any]:
    """The reviewer of a fixture whose pull.json names a manifest: a repository reviewer with no trusted ref, so its
    manifest and files are read from the fixture's base commit, as a configured repository's would be."""
    return {
        "id": "fixture",
        "protocol_version": 1,
        "trusted_ref": None,
        "scope": "repository",
        "manifest_path": manifest_path,
        "skill": None,
    }


def _configured_entry(
    config: dict[str, Any], repository: str, *, re_review: bool, scope: str | None, force: bool, canary: bool
) -> dict[str, Any]:
    """The repository's configuration entry, once the request's options fit together: a canary is an unforced initial
    review, and a re-review, and only a re-review, names a known scope."""
    if canary and (force or re_review):
        raise PipelineError("A canary is an initial review and cannot be forced")
    if re_review != (scope is not None) or scope not in (None, *RE_REVIEW_SCOPES):
        raise PipelineError(f"A re-review, and only a re-review, takes a scope: {', '.join(RE_REVIEW_SCOPES)}")
    entry = config["repositories"].get(repository)
    if entry is None:
        raise PipelineError(f"{repository} is not a configured repository")
    return entry


def _repository_reviewer(
    repository: str, config_path: Path | None, services: Services
) -> tuple[Path, str, dict[str, Any], Path, str]:
    """The configuration file, the repository's identity, its configured entry, its checkout, and the configured
    runtime, once the entry names a repository reviewer and the checkout's origin is the repository."""
    config_path = (config_path or default_config_path()).resolve()
    config = load_config(config_path)
    repository = validate_repository_identity(repository)
    entry = config["repositories"].get(repository)
    if entry is None:
        raise PipelineError(f"{repository} is not a configured repository")
    if entry["reviewer"]["scope"] != "repository":
        raise PipelineError(f"{repository} uses the suite's generic reviewer; it has no repository reviewer")
    checkout = Path(entry["checkout_path"])
    verify_checkout_remote(checkout, repository, services.git)
    return config_path, repository, entry, checkout, config["runtime"]


def _reviewer_commit(checkout: Path, reviewer: dict[str, Any], ref: str | None, services: Services) -> str:
    """The commit a reviewer is read from without a pull request: --ref, the trusted ref, or origin's default."""
    candidate = ref or reviewer["trusted_ref"] or "refs/remotes/origin/HEAD"
    try:
        return resolve_reviewer_commit(checkout, candidate, head_sha="", runner=services.git)
    except RuntimeContractError as exc:
        raise PipelineError(f"Cannot resolve {candidate} in {checkout}; pass --ref: {exc}") from exc


def inspect_reviewer(
    repository: str, *, ref: str | None = None, config_path: Path | None = None, services: Services | None = None
) -> list[str]:
    """Whether a repository's review skill can run as one entrypoint reviewer or needs a specialists manifest."""
    services = services or Services()
    config_path, repository, entry, checkout, _ = _repository_reviewer(repository, config_path, services)
    reviewer = entry["reviewer"]
    commit = _reviewer_commit(checkout, reviewer, ref, services)
    lines = [f"REVIEWER {reviewer['id']} {repository} commit={commit}"]
    if reviewer["manifest_path"]:
        return [*lines, f"MANIFEST repository {reviewer['manifest_path']}", "VERDICT manifest-configured"]
    inspection = inspect_configured_skill(checkout, commit, reviewer["skill"], services.git)
    lines.append(f"SKILL {inspection.skill}")
    lines.append("TOOLS " + (", ".join(inspection.tools) if inspection.tools is not None else "inherited (all)"))
    lines.append(f"DELEGATES {inspection.delegates} {inspection.reason}")
    lines.extend(f"EVIDENCE {number} {text}" for number, text in inspection.evidence)
    lines.extend(f"REFERENCES {path}" for path in inspection.references)
    local = manifest_location(reviewer, config_path, repository)
    if local is not None:
        lines.append(f"MANIFEST local {local} {'present' if local.is_file() else 'missing'}")
        verdict = "manifest-configured"
    else:
        verdict = {"yes": "manifest-required", "no": "entrypoint-ok", "unknown": "undetermined"}[inspection.delegates]
    lines.append(f"VERDICT {verdict}")
    return lines


def _unmatched_patterns(manifest: dict[str, Any], files: set[str]) -> list[str]:
    """Specialist include patterns that match no file in the repository: usually a typo or a renamed folder."""
    unmatched = []
    for specialist in manifest.get("specialists", []):
        excludes = [re.compile(pattern) for pattern in specialist["exclude"]]
        for pattern in specialist["include"]:
            compiled = re.compile(pattern)
            if not any(compiled.search(path) and not any(e.search(path) for e in excludes) for path in files):
                unmatched.append(f"UNMATCHED {specialist['id']} {pattern}")
    return unmatched


def run_source_example() -> Path:
    """A path as long as the source folder of a run prepare creates, which tempfile names with RUN_PREFIX and eight
    random characters, so a measurement leaves each path the room prepare's snapshot will."""
    return Path(tempfile.gettempdir()).resolve() / f"{RUN_PREFIX}{'x' * 8}" / "source"


def _excluded_reviewer_files(manifest: dict[str, Any], exclude: Sequence[str]) -> list[str]:
    """Each file the reviewer declares that a snapshot_exclude glob matches, as `<pattern> <path>`."""
    return [
        f"{pattern} {path}"
        for pattern in exclude
        for path in declared_reviewer_files(manifest)
        if glob_matcher(pattern)(path)
    ]


def _snapshot_line(
    checkout: Path,
    commit: str,
    changed: list[str],
    services: Services,
    exclude: Sequence[str] = (),
    reads: Sequence[str] | None = (),
    unreviewed_suffixes: Sequence[str] = (),
) -> str:
    """The source snapshot prepare would write for this commit on the route `_route_reads` gave, or the reason prepare
    would refuse it: lazy, writing the changed files, the analyzer settings, and the paths the `reads` patterns
    match, and listing the rest as fetchable, or whole when `reads` is None, probing as prepare probes with the
    repository's `unreviewed_suffixes`."""
    size = measure_source_snapshot(
        checkout,
        commit,
        destination=run_source_example(),
        runner=services.git,
        changed_paths=changed,
        exclude=exclude,
        upfront=None if reads is None else lazy_upfront(reads),
        unreviewed_suffixes=unreviewed_suffixes,
    )
    source = "checkout" if reads is None else "checkout-lazy"
    error = size.limit_error()
    if error:
        raise PipelineError(f"The {source} source snapshot of {commit[:12]} cannot be prepared: {error}")
    excluded = ",".join(f"{reason}:{count}" for reason, count in sorted(size.excluded.items())) or "none"
    return (
        f"SNAPSHOT {commit[:12]} source={source} files={size.files} fetchable={size.fetchable} bytes={size.bytes} "
        f"limit={MAX_SOURCE_SNAPSHOT_BYTES} excluded={excluded}"
    )


def _route_condition(
    name: str,
    *,
    checkout: Path,
    repository: str,
    head: str,
    changed: list[str],
    renamed: Mapping[str, Sequence[str]],
    reviewer_root: Path,
    manifest: dict[str, Any],
    source: Path,
    work: Path,
    diff_file: Path,
    results: dict[str, bool],
    services: Services,
    exclude: Sequence[str] = (),
) -> bool:
    """Evaluate one routing condition on the pull request's source snapshot, taken on first use as a review takes it,
    and record it: lazy, holding the paths the conditions routing runs declare they read, when each declares them.
    The script is given the pull request's diff, `diff_file`, and a file to write, as a review gives them."""
    if not source.exists():
        reads = condition_reads(manifest, changed, renamed)
        materialize_source_snapshot(
            checkout,
            repository,
            head,
            source,
            runner=services.git,
            changed_paths=changed,
            upfront=None if reads is None else lazy_upfront(reads),
            exclude=exclude,
        )
    work.mkdir(exist_ok=True)
    output = work / f"{name}.txt"
    script = manifest["conditions"][name]["script"]
    results[name] = evaluate_condition(reviewer_root, script, source, work, diff_file=diff_file, output=output)
    return results[name]


def validate_reviewer(
    repository: str,
    *,
    pulls: list[int] | None = None,
    ref: str | None = None,
    host: str | None = None,
    config_path: Path | None = None,
    services: Services | None = None,
) -> list[str]:
    """Prove a repository reviewer works without starting a reviewer or writing anything.

    Checks the manifest's structure, that every file it names exists (profiles at the trusted commit, condition
    scripts beside a local manifest), that no snapshot_exclude glob matches one of them, which include patterns
    match nothing, and, for each pull request, where prepare would read its reviewer, the snapshot prepare would
    write on the route it would take, and which specialists its changes would start, with each condition script's
    real result against the pull's head. `host` decides an auto runtime as prepare's does; only an entrypoint
    reviewer's route depends on the runtime.
    """
    services = services or Services()
    config_path, repository, entry, checkout, runtime = _repository_reviewer(repository, config_path, services)
    reviewer, exclude, suffixes = entry["reviewer"], entry["snapshot_exclude"], entry["unreviewed_suffixes"]
    default_effort = load_config(config_path)["reviewer_effort"]
    targets = _validation_targets(repository, checkout, reviewer, pulls, ref, config_path, services)
    lines: list[str] = []
    checked: set[str] = set()
    with tempfile.TemporaryDirectory(prefix="code-review-validate-") as temporary:
        scratch = Path(temporary)
        for index, target in enumerate(targets):
            pull = target.pull
            diff_file = scratch / f"diff-{index}.patch"
            changed, renamed, pull_lines = _pull_changes(repository, checkout, target, diff_file, services)
            if target.commit is None or target.resolved is None:
                # The suite's generic reviewer stands in, on the lazy snapshot prepare gives it.
                lines.extend(pull_lines)
                if pull is not None:
                    lines.append(
                        _snapshot_line(
                            checkout, pull["headRefOid"], changed, services, exclude, unreviewed_suffixes=suffixes
                        )
                    )
                lines.append(f"GENERIC files={len(changed)} (the suite's generic reviewer reviews it)")
                continue
            commit, manifest = target.commit, target.resolved.manifest
            root = scratch / f"reviewer-{index}"
            hashes = materialize_reviewer(
                checkout,
                commit,
                manifest,
                root,
                runner=services.git,
                guideline_commit=pull["baseRefOid"] if pull else None,
                local_root=target.resolved.local_root,
            )
            kind = "specialists" if manifest.get("kind") == "specialists" else "entrypoint"
            excluded = _excluded_reviewer_files(manifest, exclude)
            if excluded:
                raise PipelineError(
                    f"snapshot_exclude of {repository} matches a file its reviewer declares, which a review must be "
                    "able to read; narrow the pattern: " + ", ".join(excluded)
                )
            if commit not in checked:
                checked.add(commit)
                lines.append(
                    f"REVIEWER {manifest['id']} {kind} source={target.resolved.source} {target.resolved.location} "
                    f"commit={commit}"
                )
                lines.append(f"FILES {len(hashes)} found")
                lines.extend(_unmatched_patterns(manifest, repository_files(checkout, commit, services.git)))
            # Only an entrypoint reviewer can run on the Copilot CLI host, whose snapshot is whole.
            dispatch = choose_dispatch(services.resolve_runtime(runtime, host), kind) if kind == "entrypoint" else None
            reads = _route_reads(dispatch, manifest, changed, renamed)
            lines.extend(pull_lines)
            if pull is None:
                lines.append(_snapshot_line(checkout, commit, [], services, exclude, reads, suffixes))
                continue
            lines.append(_snapshot_line(checkout, pull["headRefOid"], changed, services, exclude, reads, suffixes))
            if kind == "entrypoint":
                lines.append(f"ENTRYPOINT {manifest['id']} files={len(changed)}")
                continue
            routing = _Routing(
                checkout,
                repository,
                pull["headRefOid"],
                root,
                scratch,
                index,
                diff_file,
                services,
                exclude,
                default_effort,
            )
            lines.extend(_routing_lines(manifest, changed, renamed, routing))
    lines.append("VALID")
    return lines


def _pull_changes(
    repository: str, checkout: Path, target: _ValidationTarget, diff_file: Path, services: Services
) -> tuple[list[str], dict[str, list[str]], list[str]]:
    """The changed paths of the pull request a validation target reviews, the earlier paths its renames give them,
    and its PULL, BODY, and NOTE lines, with its diff, read as prepare reads it on the checkout route, written to
    `diff_file`; none of them for a target without one."""
    pull = target.pull
    if pull is None:
        return [], {}, []
    diff = read_pull_diff(repository, pull["number"], pull, checkout, services)
    parsed = parse_unified_diff(diff.text)
    atomic_write_text(diff_file, diff.text)
    lines = [
        f"PULL {repository}#{pull['number']} base={pull['baseRefOid'][:12]} "
        f"head={pull['headRefOid'][:12]} files={len(parsed)} reviewer={target.source}",
        _body_line(pull.get("body", "")),
        *(f"NOTE {note}" for note in target.notes),
        *([] if diff.fallback is None else [f"NOTE {describe_fallback(diff.fallback)}"]),
    ]
    return list(parsed), renamed_from(parsed), lines


def _body_line(body: str) -> str:
    """How much of the pull request's description `prepare` would give its reviewers."""
    return f"BODY characters={len(body)} given={min(len(body), PULL_REQUEST_BODY_CHARACTERS)}"


class _Routing(NamedTuple):
    """Where validate-reviewer evaluates one pull request's routing conditions, as a review would."""

    checkout: Path
    repository: str
    head: str
    reviewer_root: Path
    scratch: Path
    target: int  # the target's position, which names its scratch folders
    diff_file: Path  # the pull request's diff, which a condition script is given
    services: Services
    exclude: Sequence[str]
    default_effort: str | None  # the configuration's reviewer_effort, which a specialist without its own effort gets


def _routing_lines(
    manifest: dict[str, Any], changed: list[str], renamed: Mapping[str, Sequence[str]], routing: _Routing
) -> list[str]:
    """What a specialists manifest does with a pull request's changes, a renamed file matched by each of its paths:
    each condition's result, each route with its settings, and the changed files no specialist matches."""
    results: dict[str, bool] = {}
    condition = functools.partial(
        _route_condition,
        checkout=routing.checkout,
        repository=routing.repository,
        head=routing.head,
        changed=changed,
        renamed=renamed,
        reviewer_root=routing.reviewer_root,
        manifest=manifest,
        source=routing.scratch / f"source-{routing.target}",
        work=routing.scratch / f"conditions-{routing.target}",
        diff_file=routing.diff_file,
        results=results,
        services=routing.services,
        exclude=routing.exclude,
    )
    routes = route(manifest, changed, condition, renamed)
    lines = [f"CONDITION {name} {'open' if value else 'closed'}" for name, value in results.items()]
    specialists = {specialist["id"]: specialist for specialist in manifest["specialists"]}
    for identity, files in routes.items():
        model, note = specialist_model(specialists[identity], routing.reviewer_root)
        # The effort a Workflow agent would start on, as `workflow` resolves it; null leaves the session's.
        effort = specialists[identity].get("effort") or routing.default_effort
        lines.append(
            f"ROUTE {identity} files={len(files)}"
            + (f" model={model}" if model else "")
            + (f" effort={effort}" if effort else "")
        )
        lines.extend([f"NOTE {note}"] if note else [])
    outside = uncovered(manifest, changed, renamed)
    if len(outside) == len(changed):
        lines.append(f"GENERIC files={len(changed)} (no specialist matched; the generic reviewer reviews it)")
        return lines
    lines.extend(f"UNCOVERED {path}" for path in outside)
    if outside and manifest.get("uncovered", "review") == "ignore":
        left = unreviewed(manifest, outside, routes)
        if left:
            lines.append(f"UNREVIEWED files={len(left)} (the manifest sets uncovered to ignore; the record lists them)")
    elif outside:
        lines.append(f"GENERIC files={len(outside)} (no specialist covers them; the generic reviewer reviews them)")
    return lines


class _ValidationTarget(NamedTuple):
    """One reviewer validate-reviewer checks: the commit it is read from and what it resolved to there, the pull
    request it reviews, the review.adapter.source prepare would record, and prepare's notes on a fallback. `commit`
    and `resolved` are None where the suite's generic reviewer stands in for the repository's."""

    commit: str | None
    resolved: ResolvedReviewer | None
    pull: dict[str, Any] | None
    source: str | None
    notes: list[str]


def _validation_targets(
    repository: str,
    checkout: Path,
    reviewer: dict[str, Any],
    pulls: list[int] | None,
    ref: str | None,
    config_path: Path,
    services: Services,
) -> list[_ValidationTarget]:
    """Each reviewer to validate, resolved before any is materialized: for each pull request, once both of its
    commits are local, the one prepare would read (the trusted ref or the base, else the default branch's tip, else
    the generic reviewer); with none, the reviewer at --ref, the trusted ref, or origin's default."""
    targets: list[_ValidationTarget] = []
    for number in pulls or []:
        fetched = validate_canary_pull(
            services.github.get_pull(repository, number), repository=repository, number=number
        )
        ensure_local_commit(checkout, fetched["headRefOid"], f"refs/pull/{number}/head", services.git)
        ensure_local_commit(checkout, fetched["baseRefOid"], f"refs/heads/{fetched['baseRefName']}", services.git)
        notes: list[str] = []
        found = _resolve_reviewer_source(
            reviewer,
            checkout=checkout,
            pull=fetched,
            config_path=config_path,
            repository=repository,
            services=services,
            notes=notes,
        )
        if found is None:
            targets.append(_ValidationTarget(None, None, fetched, "generic-fallback", notes))
        else:
            source, commit, resolved = found
            targets.append(_ValidationTarget(commit, resolved, fetched, source, notes))
    if not targets:
        commit = _reviewer_commit(checkout, reviewer, ref, services)
        resolved = resolve_reviewer(
            reviewer,
            checkout=checkout,
            commit=commit,
            config_path=config_path,
            repository=repository,
            runner=services.git,
        )
        targets.append(_ValidationTarget(commit, resolved, None, None, []))
    return targets
