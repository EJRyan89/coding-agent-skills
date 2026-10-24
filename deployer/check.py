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
CODEX_SANDBOX_SECTION = '"Windows sandbox mode" in docs/codex-support.md'
PYTHON_ALIAS_SECTION = '"Python app execution aliases" in docs/codex-support.md'
POWERSHELL_ALIAS_SECTION = '"PowerShell app execution alias" in docs/codex-support.md'
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
        # A tool every root using it is an unchosen opt-in item for is optional, like a tool marked optional or one
        # that every skill using it declares optional. An opt-in item is chosen when the requested roots stand for it,
        # not when it is installed, so a tool another root also uses stays needed.
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


def _setting_lines() -> list[ReportLine]:
    """The machine settings a runtime needs before it can run a skill's scripts, where this platform has them: what
    the skills' Python command runs, what pwsh runs where there is one, and, when Codex CLI is installed and meets its
    floor, the sandbox it would use."""
    lines: list[ReportLine] = []
    if platform_support.HAS_APP_EXECUTION_ALIASES:
        lines.append(_python_command_line())
        pwsh = platform_support.find_executable("pwsh")
        if pwsh is not None:
            lines.append(_pwsh_command_line(pwsh))
    if platform_support.CODEX_NEEDS_SANDBOX_SETTING:
        codex = tools.probe(tools.CODEX)
        if codex.path is not None and codex.refusal is None and not codex.outdated:
            lines.append(_codex_sandbox_line(codex.path))
    return lines


def _python_command_line() -> ReportLine:
    label = f"{platform_support.PYTHON} command"
    program = platform_support.find_executable(platform_support.PYTHON)
    if program is None:
        return ReportLine(label, "MISSING", f"every skill's scripts run {platform_support.PYTHON}; put Python on PATH")
    if platform_support.is_app_execution_alias(program):
        return ReportLine(
            label,
            "UNUSABLE",
            f"runs the app execution alias {platform_support.normalize(program)}, which Codex CLI cannot start; "
            f"{platform_support.APP_EXECUTION_ALIAS_REMEDY}; see {PYTHON_ALIAS_SECTION}",
        )
    return ReportLine(label, "FOUND", f"runs {platform_support.normalize(program)}, not an app execution alias")


def _pwsh_command_line(program: str) -> ReportLine:
    """Codex CLI runs every command through the pwsh it finds, and Windows PowerShell only when there is none, so a
    pwsh that is an app execution alias, the packaged PowerShell's, stops every command; with no pwsh, nothing does."""
    label = "pwsh command"
    if platform_support.is_app_execution_alias(program):
        return ReportLine(
            label,
            "UNUSABLE",
            f"runs the app execution alias {platform_support.normalize(program)}, the packaged PowerShell, which "
            f"Codex CLI runs every command through and cannot start; {platform_support.POWERSHELL_ALIAS_REMEDY}; "
            f"see {POWERSHELL_ALIAS_SECTION}",
        )
    return ReportLine(label, "FOUND", f"runs {platform_support.normalize(program)}, not an app execution alias")


def _codex_sandbox_line(codex: str) -> ReportLine:
    label = "codex sandbox"
    mode = tools.codex_sandbox_mode(codex)
    if mode == tools.CODEX_WRITABLE_SANDBOX:
        # The configured mode only: running a command in the sandbox could start the elevated sandbox's setup, which
        # asks for an administrator's approval, so the section gives that model-free command to run by hand.
        return ReportLine(
            label, "FOUND", f"{mode} configured; confirm its sandbox starts a command as {CODEX_SANDBOX_SECTION} shows"
        )
    found = (
        f"{mode}, so Codex CLI blocks every skill script"
        if mode is not None
        else "codex debug prompt-input named no sandbox mode"
    )
    return ReportLine(
        label,
        "UNUSABLE",
        f"{found}; set [features] prefer_mxc or [windows] sandbox in ~/.codex/config.toml; see {CODEX_SANDBOX_SECTION}",
    )


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
        skill_lines = [*_skill_lines(src, owned), *_setting_lines()]
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
        print("until it is installed, updated, or set up as its line says.")
    else:
        print("Ready to deploy.")
    print("")
    return 0
