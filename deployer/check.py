"""The read-only check command: report each prerequisite, its version, and what needs it."""

from __future__ import annotations

import argparse

from pdf_tool import skipped_text

from . import manifest, platform_support, source, source_commit, tools
from .arguments import CHECK_COMMAND, parse_command
from .errors import DeployError, debug_requested, fail
from .paths import Paths
from .report import ReportLine, currently_chosen, print_report, warn_ignored_home

CHECK_ACTIONS = ("MISSING", "OUTDATED", "UNUSABLE", "FOUND", "OPTIONAL")
NEEDED_TO_DEPLOY = "needed to deploy"
# What check asks for when a tool needed to deploy is missing, outdated, or both, keyed by (missing, outdated).
BLOCKED_LEADS = {
    (True, False): "Install the missing tools",
    (False, True): "Update the outdated tools",
    (True, True): "Install the missing tools and update the outdated ones",
}


def _version_text(probe: tools.Probe) -> str:
    if probe.pdf_identity is not None:
        return f" {probe.pdf_identity.provider} {probe.pdf_identity.version_text}"
    return f" {tools.format_version(probe.version)}" if probe.version else ""


def _deploy_lines() -> list[ReportLine]:
    python = tools.python_version()
    minimum = tools.format_version(tools.MINIMUM_PYTHON)
    label = f"Python {tools.format_version(python)}"
    if python < tools.MINIMUM_PYTHON:
        lines = [ReportLine(label, "OUTDATED", f"{NEEDED_TO_DEPLOY}; needs {minimum} or newer")]
    else:
        lines = [ReportLine(label, "FOUND", f"{NEEDED_TO_DEPLOY}; {minimum} or newer")]
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
        lines.append(_tool_line(tool, optional, named))
    lines.extend(_tool_line(tool, tool.optional, []) for tool in tools.REPORTED_TOOLS)
    return lines


def _tool_line(tool: tools.Tool, optional: bool, roots: list[str]) -> ReportLine:
    used_by = f"used by {', '.join([*roots, *tool.other_uses])}"
    probe = tools.probe(tool)
    if probe.path is None:
        hint = "" if tool.install_hint is None else f"; {tool.install_hint}"
        missing = ("OPTIONAL", f"not installed; {used_by}{hint}") if optional else ("MISSING", f"{used_by}{hint}")
        return ReportLine(tool.label, *missing)
    if probe.refusal is not None:
        return ReportLine(
            f"{tool.label}{_version_text(probe)}", "OUTDATED" if probe.outdated else "UNUSABLE", probe.refusal
        )
    if probe.outdated:
        minimum = tools.format_version(tool.minimum)
        return ReportLine(f"{tool.label}{_version_text(probe)}", "OUTDATED", f"needs {minimum} or newer")
    found = "OPTIONAL" if optional else "FOUND"
    detail = f"{used_by}; {skipped_text(probe.skipped)}" if probe.skipped else used_by
    return ReportLine(f"{tool.label}{_version_text(probe)}", found, detail)


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
        recorded = manifest.load(paths.manifest_file)
        owned = recorded.ownership(source_id)
        deployed = source_commit.describe(paths.source_dir, recorded.source_commit(source_id))
        pinned = recorded.source_ref(source_id)
        deploy_lines = _deploy_lines()
        skill_lines = _skill_lines(src, owned)
    except (DeployError, OSError, KeyboardInterrupt) as exc:
        return fail(exc, debug, "finish the check")
    print("")
    print(f"Source: {source.label(src.source_id, src.name)}")
    print(f"Deployed commit: {deployed}" + (f", pinned with --at {pinned}" if pinned is not None else ""))
    print(f"Home: {platform_support.normalize(paths.home)}")
    warn_ignored_home(paths.home)
    print_report("CHECK", CHECK_ACTIONS, [*deploy_lines, *skill_lines])
    actions = {line.action for line in deploy_lines}
    lead = BLOCKED_LEADS.get(("MISSING" in actions, "OUTDATED" in actions))
    if lead is not None:
        print(f'{lead} before deploying; see "Installing the tools" in')
        print("docs/installation.md. Open a new terminal afterwards so the tools are on PATH.")
        print("")
        return 1
    if any(line.action in ("MISSING", "OUTDATED", "UNUSABLE") for line in skill_lines):
        print("Ready to deploy. Skills cannot use a missing, outdated, or unusable tool")
        print("until it is installed or updated.")
    else:
        print("Ready to deploy.")
    print("")
    return 0
