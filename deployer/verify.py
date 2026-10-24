"""The read-only verify command: check that Codex and Copilot CLI find every deployed runtime adapter."""

from __future__ import annotations

import argparse
import dataclasses
import os
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path

from . import discovery, manifest, platform_support, tools
from .arguments import PROG, VERIFY_COMMAND, parse_command
from .discovery import Listed, Listing, ListingError, Refused
from .errors import DeployError, debug_requested, fail
from .kinds import ADAPTER_KIND
from .paths import Paths, copilot_home
from .render import CODEX_METADATA
from .report import ReportLine, copilot_home_note, paragraph, print_report

# Filesystem writes that tests/run_validation.py allows outside deployer/fsops.py, with the reason.
FSOPS_ALLOWED = {
    "tempfile.mkdtemp": "verify starts each runtime in an empty directory under the system temporary directory, "
    "outside every managed root; the read-only command changes no deployed state",
    "shutil.rmtree": "verify removes only the empty working directory it created with tempfile.mkdtemp",
}
VERIFY_ACTIONS = ("NOT FOUND", "DISABLED", "SHADOWED", "EXPOSED", "OMITTED", "FOUND")
# What an adapter only the user may start is, where Codex offers it to the model anyway.
EXPOSED_DETAIL = f"Codex offers it to the model, though only the user starts it; check its {CODEX_METADATA}"
# What an adapter the model may start is, where Codex lists it but leaves it out of the model's catalog, as its skills
# budget does once the skills' names and paths alone overrun it.
OMITTED_DETAIL = "Codex lists it but leaves it out of the model's catalog; check its skills.max_context_tokens"
# The lines of an adapter Copilot CLI did not list: it lists one copy per name, the one it will use, so a SHADOWED
# adapter is one it listed another copy in place of, as it does for a same-named skill under COPILOT_HOME.
UNLISTED = ("NOT FOUND", "SHADOWED")
SUPPORT = {"codex": "docs/codex-support.md", "copilot": "docs/copilot-support.md"}
# The line that names each runtime verify failed for, which the deployable workflow's report reads back.
FAILED_FOR = "Verification failed for "
# Each probe's failure has its own sentence, so a reader knows which command to rerun by itself.
LISTING_FAILED = "Cannot list its skills: "
CATALOG_FAILED = (
    f"Listed its skills, but cannot read which ones it offers the model (codex {' '.join(discovery.CODEX_CATALOG)}): "
)


def _section(label: str, text: str) -> None:
    """A runtime's section without a report, laid out like print_report's."""
    print("")
    print(f"=== {label.upper()} ===")
    print("")
    print(text)
    print("")


def adapter_names(owned: manifest.Manifest) -> list[str]:
    """Every runtime adapter the manifest records, from every source."""
    names: set[str] = set()
    for entry in owned.sources.values():
        if isinstance(entry, dict):
            names.update((entry.get(ADAPTER_KIND.key) or {}).keys())
    return sorted(names)


def _same(directory: str, expected: Path) -> bool:
    """Whether a runtime's listed directory is the expected one, however it spells the path.

    A runtime may list a profile folder by its 8.3 short name (RUNNER~1 for runneradmin) or through a link, so compare
    the directories they resolve to, not the text.
    """
    return platform_support.same_directory(Path(directory), expected)


def adapter_line(name: str, copies: list[Listed], expected: Path, refused: tuple[Refused, ...] = ()) -> ReportLine:
    """FOUND only when the runtime lists the adapter, enabled, and no other enabled copy of its name; NOT FOUND names
    the runtime's reason when it found the adapter but could not load it."""
    ours = [copy for copy in copies if _same(copy.directory, expected)]
    others = [copy.directory for copy in copies if copy.enabled and copy not in ours]
    if others:
        return ReportLine(name, "SHADOWED", f"also {', '.join(others)}")
    if not ours:
        reasons = [refusal.reason for refusal in refused if _same(refusal.directory, expected)]
        return ReportLine(name, "NOT FOUND", f"could not load it: {'; '.join(reasons)}" if reasons else "")
    if not any(copy.enabled for copy in ours):
        return ReportLine(name, "DISABLED", "turned off in the runtime's settings")
    return ReportLine(name, "FOUND")


def runtime_lines(
    names: list[str], listing: Listing, adapters: Path, refused: tuple[Refused, ...] = ()
) -> list[ReportLine]:
    return [adapter_line(name, listing.get(name, []), adapters / name, refused) for name in names]


def user_only(names: list[str], adapters: Path) -> list[str]:
    """The adapters whose agents/openai.yaml, which the deployer writes only for a skill the user alone starts, keeps
    Codex from offering the skill to the model."""
    return [name for name in names if (adapters / name / CODEX_METADATA).is_file()]


def catalogued(lines: list[ReportLine], hidden: list[str], catalog: Listing, adapters: Path) -> list[ReportLine]:
    """The lines with each FOUND adapter of `hidden` that the model's catalog offers from its directory EXPOSED, and
    each other FOUND adapter that it does not offer from there OMITTED."""
    return [_catalogued(line, line.name in hidden, catalog, adapters) for line in lines]


def _catalogued(line: ReportLine, only_user: bool, catalog: Listing, adapters: Path) -> ReportLine:
    if line.action != "FOUND":
        return line
    offered = any(_same(copy.directory, adapters / line.name) for copy in catalog.get(line.name, []))
    if only_user and offered:
        return ReportLine(line.name, "EXPOSED", EXPOSED_DETAIL)
    if not only_user and not offered:
        return ReportLine(line.name, "OMITTED", OMITTED_DETAIL)
    return line


def unlisted_note(runtime: str, lines: list[ReportLine], paths: Paths, environment: Mapping[str, str]) -> str | None:
    """Why Copilot CLI listed none of the adapters, whether it listed nothing or a same-named skill in place of one,
    when the COPILOT_HOME it ran with is the reason: Copilot then reads personal skills from that directory's skills
    folder alone, unless the variable names its default."""
    copilot = dataclasses.replace(paths, copilot_home=copilot_home(environment))
    if runtime != "copilot" or copilot.copilot_home is None or copilot.copilot_reads_adapters:
        return None
    if any(line.action not in UNLISTED for line in lines):
        return None
    return copilot_home_note(copilot.copilot_home, paths.adapter_dest_dir, paths.default_copilot_dir)


def runtime_report(
    runtime: str, executable: str, names: list[str], adapters: Path, cwd: Path, environment: Mapping[str, str]
) -> list[ReportLine] | str:
    """The runtime's report lines, or the sentence that says which of its probes failed: the listing, or, for Codex,
    the catalog of the skills it offers the model."""
    try:
        discovered = discovery.discover(runtime, executable, cwd, environment)
    except ListingError as exc:
        return f"{LISTING_FAILED}{exc}"
    lines = runtime_lines(names, discovered.listing, adapters, discovered.refused)
    if runtime != "codex":
        return lines
    try:
        catalog = discovery.codex_catalog(executable, cwd, environment)
    except ListingError as exc:
        return f"{CATALOG_FAILED}{exc}"
    return catalogued(lines, user_only(names, adapters), catalog, adapters)


def run(arguments: list[str], paths: Paths, environment: Mapping[str, str] | None = None) -> int:
    """`python deploy.py verify` with these arguments."""
    namespace = parse_command([VERIFY_COMMAND, *arguments])
    return namespace if isinstance(namespace, int) else execute(namespace, paths, environment)


def execute(namespace: argparse.Namespace, paths: Paths, environment: Mapping[str, str] | None = None) -> int:
    debug = debug_requested(namespace.debug)
    try:
        platform_support.ensure_supported()
        names = adapter_names(manifest.load(paths.manifest_file))
        if not names:
            where = platform_support.normalize(paths.adapter_dest_dir)
            raise DeployError(f"ERROR: No runtime adapters are deployed in {where}.", f"Deploy first with '{PROG}'.")
        return _verify(names, paths, dict(os.environ if environment is None else environment))
    except (DeployError, OSError, KeyboardInterrupt) as exc:
        return fail(exc, debug, "finish verifying the adapters")


def _verify(names: list[str], paths: Paths, environment: dict[str, str]) -> int:
    adapters = paths.adapter_dest_dir
    print("")
    print(f"Adapters: {len(names)} in {platform_support.normalize(adapters)}")
    verified: list[str] = []
    problems: list[str] = []
    # An empty working directory, so no project skill stands in for an adapter.
    workdir = Path(tempfile.mkdtemp(prefix="deploy-verify-"))
    try:
        for runtime in discovery.RUNTIMES:
            label = discovery.LABELS[runtime]
            probe = tools.probe(tools.VERIFY_TOOLS[runtime])
            if probe.path is None:
                _section(label, "Not installed; skipped.")
                continue
            if probe.outdated and probe.version is not None:
                version, minimum = tools.format_version(probe.version), tools.format_version(probe.tool.minimum)
                _section(
                    label,
                    f"OUTDATED: {label} {version} is older than {minimum}, the oldest version verify "
                    "can read. Update it, then rerun.",
                )
                problems.append(runtime)
                continue
            lines = runtime_report(runtime, probe.path, names, adapters, workdir, environment)
            if isinstance(lines, str):
                _section(label, lines)
                problems.append(runtime)
                continue
            print_report(label.upper(), VERIFY_ACTIONS, lines)
            note = unlisted_note(runtime, lines, paths, environment)
            if note is not None:
                print(paragraph(note))
                print("")
            (verified if all(line.action == "FOUND" for line in lines) else problems).append(runtime)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    if problems:
        print(f"{FAILED_FOR}{' and '.join(discovery.LABELS[runtime] for runtime in problems)}.")
        print(f"See {' and '.join(SUPPORT[runtime] for runtime in problems)}.")
        print("")
        return 1
    if not verified:
        print("Neither Codex CLI nor Copilot CLI is installed, so nothing was verified.")
        print("")
        return 1
    found = " and ".join(discovery.LABELS[runtime] for runtime in verified)
    print(f"Verified: {found} {'find' if len(verified) > 1 else 'finds'} every adapter.")
    print("")
    return 0
