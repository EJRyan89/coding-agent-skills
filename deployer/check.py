"""The read-only check command: report each prerequisite, its version, and what needs it."""

from __future__ import annotations

import argparse

from . import manifest, platform_support, source, tools
from .arguments import CHECK_COMMAND, parse_command
from .errors import DeployError, debug_requested, fail
from .paths import Paths
from .report import ReportLine, currently_chosen, print_report, warn_ignored_home

CHECK_ACTIONS = ("MISSING", "OUTDATED", "FOUND", "OPTIONAL")
NEEDED_TO_DEPLOY = "needed to deploy"


def _version_text(probe: tools.Probe) -> str:
    return f" {tools.format_version(probe.version)}" if probe.version else ""


def _deploy_lines() -> list[ReportLine]:
    python = tools.python_version()
    minimum = tools.format_version(tools.MINIMUM_PYTHON)
    lines = [ReportLine(f"Python {tools.format_version(python)}", "FOUND", f"{NEEDED_TO_DEPLOY}; {minimum} or newer")]
    for tool in tools.DEPLOY_TOOLS:
        probe = tools.probe(tool)
        if probe.path is None:
            hint = platform_support.install_hint(tool.label)
            lines.append(ReportLine(tool.label, "MISSING", f"{NEEDED_TO_DEPLOY}; {hint}"))
        elif tool.name == "git-bash":
            lines.append(
                ReportLine(tool.label, "FOUND", f"{NEEDED_TO_DEPLOY}; {platform_support.normalize(probe.path)}")
            )
        elif tool.name == "powershell" and probe.version is not None and probe.version < (7,):
            detail = "enough to deploy; the validation suite needs PowerShell 7"
            lines.append(ReportLine(f"{tool.label}{_version_text(probe)}", "FOUND", detail))
        else:
            lines.append(ReportLine(f"{tool.label}{_version_text(probe)}", "FOUND", NEEDED_TO_DEPLOY))
    return lines


def _skill_lines(src: source.Source, owned: manifest.Ownership) -> list[ReportLine]:
    users = source.tool_users(src, sorted(src.bundles), source.root_names(src))
    required = source.required_tools(src, sorted(src.bundles), source.root_names(src))
    # The runtimes verify runs are reported even when no skill declares them.
    catalogue = {**tools.VERIFY_TOOLS, **tools.SKILL_TOOLS}
    users |= {name: [] for name in tools.VERIFY_TOOLS if name not in users}
    lines: list[ReportLine] = []
    for name, roots in users.items():
        tool = catalogue[name]
        # An opt-in item that is not installed makes its tools optional, like a tool marked optional or one that
        # every skill using it declares optional.
        dormant = {
            root
            for root in roots
            if source.is_opt_in(src, root)
            and not currently_chosen(src, owned, root, "bundle" if root in src.bundles else "skill")
        }
        optional = tool.optional or dormant == set(roots) or name not in required
        named = [f"opt-in {root}" if root in dormant else root for root in roots]
        used_by = f"used by {', '.join([*named, *tool.other_uses])}"
        probe = tools.probe(tool)
        if probe.path is None:
            missing = ("OPTIONAL", f"not installed; {used_by}") if optional else ("MISSING", used_by)
            lines.append(ReportLine(tool.label, *missing))
        elif probe.outdated:
            minimum = tools.format_version(tool.minimum)
            lines.append(ReportLine(f"{tool.label}{_version_text(probe)}", "OUTDATED", f"needs {minimum} or newer"))
        else:
            found = "OPTIONAL" if optional else "FOUND"
            lines.append(ReportLine(f"{tool.label}{_version_text(probe)}", found, used_by))
    return lines


def run(arguments: list[str], paths: Paths) -> int:
    """`python deploy.py check` with these arguments."""
    namespace = parse_command([CHECK_COMMAND, *arguments])
    return namespace if isinstance(namespace, int) else execute(namespace, paths)


def execute(namespace: argparse.Namespace, paths: Paths) -> int:
    debug = debug_requested(namespace.debug)
    try:
        platform_support.ensure_supported()
        source_id = source.load_source_id(paths)
        src = source.discover(paths, source_id)
        owned = manifest.load(paths.manifest_file).ownership(source_id)
        deploy_lines = _deploy_lines()
        skill_lines = _skill_lines(src, owned)
    except (DeployError, OSError, KeyboardInterrupt) as exc:
        return fail(exc, debug, "finish the check")
    print("")
    print(f"Source: {source.label(src.source_id, src.name)}")
    print(f"Home: {platform_support.normalize(paths.home)}")
    warn_ignored_home(paths.home)
    print_report("CHECK", CHECK_ACTIONS, [*deploy_lines, *skill_lines])
    if any(line.action == "MISSING" for line in deploy_lines):
        print('Install the missing tools before deploying; see "Installing the tools" in')
        print("docs/installation.md. Open a new terminal afterwards so the tools are on PATH.")
        print("")
        return 1
    if any(line.action in ("MISSING", "OUTDATED") for line in skill_lines):
        print("Ready to deploy. Skills that use a missing or outdated tool will fail")
        print("until it is installed or updated.")
    else:
        print("Ready to deploy.")
    print("")
    return 0
