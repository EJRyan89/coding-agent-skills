"""The external tools the deployer and the skills run, and how to find them and read their versions."""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from dataclasses import dataclass

from pdf_tool import (
    MINIMUMS,
    PdfIdentity,
    PdfProvider,
    PdfToolRefused,
    Skipped,
    install_text,
    probe_pdftotext,
    select_pdftotext,
)

from . import platform_support

# 3.11.4 is the first 3.11 release whose tarfile has extraction filters, which --at extracts a commit's tree with.
MINIMUM_PYTHON = (3, 11, 4)
VERSION = re.compile(r"\d+\.\d+(?:\.\d+)?")


@dataclass(frozen=True)
class Tool:
    """A tool by its deploy-meta name, how to locate it, and its version or provider support requirement, if any."""

    name: str
    label: str
    locate: Callable[[], str | None]
    version_arguments: tuple[str, ...] = ("--version",)
    minimum: tuple[int, ...] = ()
    optional: bool = False
    other_uses: tuple[str, ...] = ()
    # The PDF parser floors, by provider, for a tool pdf_tool probes; it owns the probe, its arguments, and the exit
    # statuses each provider is allowed, so such a tool sets nothing else about its version. It chooses among every
    # program the locator finds by the tool's name, as the extraction does, so `locate` only says one is installed.
    provider_minimum: dict[PdfProvider, tuple[int, ...]] | None = None
    # What `deploy.py check` adds when the tool is not installed, as each refusal of it does: what to install on this
    # platform, or None.
    install_hint: str | None = None


POWERSHELL_VERSION = ("-NoProfile", "-NonInteractive", "-Command", "$PSVersionTable.PSVersion.ToString()")

DEPLOY_TOOLS = (
    Tool("git-bash", platform_support.BASH_NAME, lambda: platform_support.find_bash(), version_arguments=()),
    Tool("shellcheck", "ShellCheck", lambda: platform_support.find_executable("shellcheck")),
    Tool("powershell", "PowerShell", lambda: platform_support.find_powershell(), POWERSHELL_VERSION),
)

# Commands every deployment already provides, so skills may run them without declaring them: Git, Bash and the
# utilities that come with it, PowerShell 7, and the platform's own, such as Python's command name. Keep in step with
# "Commands skills may run" in docs/adding-a-skill.md.
STANDARD_COMMANDS = (
    frozenset(
        {
            "git",
            "bash",
            "sh",
            "pwsh",
            "awk",
            "basename",
            "cat",
            "cmp",
            "comm",
            "cp",
            "curl",
            "cut",
            "date",
            "diff",
            "dirname",
            "env",
            "expr",
            "find",
            "grep",
            "gzip",
            "head",
            "ls",
            "mkdir",
            "mktemp",
            "mv",
            "od",
            "paste",
            "readlink",
            "realpath",
            "rm",
            "rmdir",
            "sed",
            "seq",
            "sleep",
            "sort",
            "stat",
            "tail",
            "tar",
            "tee",
            "touch",
            "tr",
            "uniq",
            "wc",
            "xargs",
        }
    )
    | platform_support.STANDARD_COMMANDS
)

COPILOT = Tool(
    "copilot",
    "copilot",
    lambda: platform_support.find_executable("copilot"),
    minimum=(1, 0, 88),
    optional=True,
    other_uses=("Copilot verification",),
)
# 0.88.0 is the first release whose app-server skills/list answer says whether each skill is enabled, which verify
# reads to report a disabled adapter.
CODEX = Tool(
    "codex",
    "codex",
    lambda: platform_support.find_executable("codex"),
    minimum=(0, 88, 0),
    optional=True,
    other_uses=("Codex verification",),
)

# The documented probe of the sandbox Codex CLI would run a skill's scripts in, which starts no model: the
# instructions it would send name the sandbox_mode in effect, workspace-write once the Windows sandbox setting is read.
CODEX_SANDBOX_PROBE = ("debug", "prompt-input", "-c", "sandbox_mode=workspace-write")
CODEX_SANDBOX_MODE = re.compile(r"sandbox_mode` is `([a-z-]+)`")
CODEX_WRITABLE_SANDBOX = "workspace-write"


def codex_sandbox_mode(codex: str) -> str | None:
    """The sandbox mode the Codex CLI at this path would run a skill's scripts in, or None when it names none.

    A probe that does not finish raises ToolTimedOut, which stops `check` as every other tool's deadline does.
    """
    result = platform_support.run_tool([codex, *CODEX_SANDBOX_PROBE])
    found = CODEX_SANDBOX_MODE.search(result.output) if result.returncode == 0 else None
    return None if found is None else found.group(1)


# Tools a skill may declare in its deploy-meta "tools" list. Python is a deployment requirement already, and Git,
# which every skill runs, is reported apart in REPORTED_TOOLS below.
SKILL_TOOLS = {
    tool.name: tool
    for tool in (
        # 2.48.0 added `gh api --paginate --slurp`, which skill scripts use to parse paginated output as JSON.
        Tool("gh", "gh", lambda: platform_support.find_executable("gh"), minimum=(2, 48, 0)),
        COPILOT,
        # The deprecated dotnet-format global tool, which formats only a .sln holding a project older than SDK-style,
        # so a skill that runs it declares it optional.
        Tool("dotnet-format", "dotnet-format", lambda: platform_support.find_executable("dotnet-format")),
        # The .NET SDK's `dotnet format` formats a .slnx solution, which the dotnet-format global tool cannot open,
        # and a .sln of SDK-style projects, whose analyzers the global tool cannot load, so a skill that formats
        # declares it required; 9.0.200 is the first SDK that reads .slnx.
        Tool("dotnet", "dotnet", lambda: platform_support.find_executable("dotnet"), minimum=(9, 0, 200)),
        # Shared maintained-release policy; check and each extraction use the same locator and bounded vendor probe.
        Tool(
            "pdftotext",
            "pdftotext",
            lambda: platform_support.find_executable("pdftotext"),
            provider_minimum=MINIMUMS,
            install_hint=install_text(),
        ),
    )
}

# The runtimes `deploy.py verify` asks for their skill listings, by their names in discovery.RUNTIMES. No skill runs
# Codex CLI, so only this catalogue names it.
VERIFY_TOOLS = {tool.name: tool for tool in (CODEX, COPILOT)}

# Tools `deploy.py check` reports so that its output alone answers a bug report: Git, which every skill runs, and
# Claude Code, which no deployment needs. Git has no floor. 2.1.295 is the first Claude Code that honors a hook's
# `onFailure: block`, which the code-review reviewer's guard declares so that a guard that cannot start, times out, or
# exits with an unexpected status denies the call; an older one ignores the setting and lets that call through.
REPORTED_TOOLS = (
    Tool("git", "Git", lambda: platform_support.find_executable("git"), other_uses=("every skill",)),
    Tool(
        "claude",
        "Claude Code",
        lambda: platform_support.find_executable("claude"),
        minimum=(2, 1, 295),
        optional=True,
        other_uses=("every skill run in Claude Code",),
    ),
)


def parse_version(text: str) -> tuple[int, ...] | None:
    match = VERSION.search(text)
    return tuple(int(part) for part in match.group(0).split(".")) if match else None


def format_version(version: tuple[int, ...]) -> str:
    return ".".join(str(part) for part in version)


@dataclass(frozen=True)
class Probe:
    tool: Tool
    path: str | None
    version: tuple[int, ...] | None
    pdf_identity: PdfIdentity | None = None
    refusal: str | None = None
    # The programs a provider probe passed over before the one it chose, or all of them when it refused.
    skipped: tuple[Skipped, ...] = ()

    @property
    def outdated(self) -> bool:
        if self.pdf_identity is not None:
            return self.pdf_identity.outdated
        return bool(self.tool.minimum) and self.version is not None and self.version < self.tool.minimum


def probe(tool: Tool) -> Probe:
    """Locate a tool and, when it reports one, read its version."""
    if tool.provider_minimum is not None:
        return _provider_probe(tool)
    path = tool.locate()
    if path is None or not tool.version_arguments:
        return Probe(tool, path, None)
    result = platform_support.run_tool([path, *tool.version_arguments])
    return Probe(tool, path, parse_version(result.output) if result.returncode == 0 else None)


def _provider_probe(tool: Tool) -> Probe:
    """The program pdf_tool chooses among every one the locator finds, as each extraction chooses it, or the first
    found with the refusal that names every one it passed over."""
    found = platform_support.find_executables(tool.name)
    if not found:
        return Probe(tool, None, None)
    try:
        selection = select_pdftotext(found, probe_pdftotext)
    except PdfToolRefused as exc:
        version = None if exc.identity is None else exc.identity.version
        return Probe(tool, found[0], version, exc.identity, str(exc))
    return Probe(tool, selection.path, selection.identity.version, selection.identity, skipped=selection.skipped)


def python_version() -> tuple[int, ...]:
    return tuple(sys.version_info[:3])
