"""The read-only verify command: check that Codex and Copilot CLI find every deployed runtime adapter."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path

from . import discovery, manifest, platform_support, tools
from .arguments import ParserExit, verify_parser
from .discovery import Listed, Listing, ListingError
from .errors import DeployError, print_error
from .paths import Paths
from .report import ReportLine, print_report

# Filesystem writes that tests/run_validation.py allows outside deployer/fsops.py, with the reason.
FSOPS_ALLOWED = {
    "tempfile.mkdtemp": "verify starts each runtime in an empty directory under the system temporary directory, "
    "outside every managed root; the read-only command changes no deployed state",
    "shutil.rmtree": "verify removes only the empty working directory it created with tempfile.mkdtemp",
}
VERIFY_ACTIONS = ("NOT FOUND", "DISABLED", "SHADOWED", "FOUND")
SUPPORT = {"codex": "docs/codex-support.md", "copilot": "docs/copilot-support.md"}


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
            names.update((entry.get(manifest.ADAPTERS) or {}).keys())
    return sorted(names)


def _same(directory: str, expected: Path) -> bool:
    """Whether a runtime's listed directory is the expected one, however it spells the path.

    A runtime may list a profile folder by its 8.3 short name (RUNNER~1 for runneradmin) or through a link, so compare
    the directories they resolve to, not the text.
    """
    return platform_support.same_directory(Path(directory), expected)


def adapter_line(name: str, copies: list[Listed], expected: Path) -> ReportLine:
    """FOUND only when the runtime lists the adapter, enabled, and no other enabled copy of its name."""
    ours = [copy for copy in copies if _same(copy.directory, expected)]
    others = [copy.directory for copy in copies if copy.enabled and copy not in ours]
    if others:
        return ReportLine(name, "SHADOWED", f"also {', '.join(others)}")
    if not ours:
        return ReportLine(name, "NOT FOUND")
    if not any(copy.enabled for copy in ours):
        return ReportLine(name, "DISABLED", "turned off in the runtime's settings")
    return ReportLine(name, "FOUND")


def runtime_lines(names: list[str], listing: Listing, adapters: Path) -> list[ReportLine]:
    return [adapter_line(name, listing.get(name, []), adapters / name) for name in names]


def run(arguments: list[str], paths: Paths, environment: Mapping[str, str] | None = None) -> int:
    try:
        verify_parser().parse_args(arguments)
        platform_support.ensure_supported()
        names = adapter_names(manifest.load(paths.manifest_file))
        if not names:
            where = platform_support.normalize(paths.adapter_dest_dir)
            raise DeployError(
                f"ERROR: No runtime adapters are deployed in {where}.", "Deploy first with 'python deploy.py'."
            )
    except ParserExit as exc:
        return exc.code
    except DeployError as exc:
        print_error(exc)
        return exc.exit_code
    environment = dict(os.environ if environment is None else environment)
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
            try:
                listing = discovery.list_skills(runtime, probe.path, workdir, environment)
            except ListingError as exc:
                _section(label, f"Cannot list its skills: {exc}")
                problems.append(runtime)
                continue
            lines = runtime_lines(names, listing, adapters)
            print_report(label.upper(), VERIFY_ACTIONS, lines)
            (verified if all(line.action == "FOUND" for line in lines) else problems).append(runtime)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    if problems:
        print(f"Verification failed for {' and '.join(discovery.LABELS[runtime] for runtime in problems)}.")
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
