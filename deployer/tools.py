"""The external tools the deployer and the skills run, and how to find them and read their versions."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from typing import Callable

from . import platform_support

MINIMUM_PYTHON = (3, 11)
VERSION = re.compile(r"\d+\.\d+(?:\.\d+)?")


@dataclass(frozen=True)
class Tool:
    """A tool by its deploy-meta name, how to locate it, and the oldest version that works, if any."""

    name: str
    label: str
    locate: Callable[[], str | None]
    version_arguments: tuple[str, ...] = ("--version",)
    minimum: tuple[int, ...] = ()
    optional: bool = False
    other_uses: tuple[str, ...] = ()


POWERSHELL_VERSION = ("-NoProfile", "-NonInteractive", "-Command", "$PSVersionTable.PSVersion.ToString()")

DEPLOY_TOOLS = (
    Tool("git-bash", "Git Bash", lambda: platform_support.find_bash(), version_arguments=()),
    Tool("shellcheck", "ShellCheck", lambda: platform_support.find_executable("shellcheck")),
    Tool("powershell", "PowerShell", lambda: platform_support.find_powershell(), POWERSHELL_VERSION),
)

# Commands every deployment already provides, so skills may run them without declaring them: Git, Bash and the
# utilities that come with it, PowerShell 7, and the platform's own, such as Python's command name. Keep in step with
# "Commands skills may run" in docs/adding-a-skill.md.
STANDARD_COMMANDS = frozenset({
    "git", "bash", "sh", "pwsh",
    "awk", "basename", "cat", "cmp", "comm", "cp", "curl", "cut", "date", "diff", "dirname", "env",
    "expr", "find", "grep", "gzip", "head", "ls", "mkdir", "mktemp", "mv", "od", "paste", "readlink", "realpath",
    "rm", "rmdir", "sed", "seq", "sleep", "sort", "stat", "tail", "tar", "tee", "touch", "tr", "uniq", "wc",
    "xargs",
}) | platform_support.STANDARD_COMMANDS

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

# Tools a skill may declare in its deploy-meta "tools" list. Git and Python are deployment requirements already.
SKILL_TOOLS = {
    tool.name: tool
    for tool in (
        # 2.48.0 added `gh api --paginate --slurp`, which skill scripts use to parse paginated output as JSON.
        Tool("gh", "gh", lambda: platform_support.find_executable("gh"), minimum=(2, 48, 0)),
        COPILOT,
        Tool("dotnet-format", "dotnet-format", lambda: platform_support.find_executable("dotnet-format")),
    )
}

# The runtimes `deploy.py verify` asks for their skill listings, by their names in discovery.RUNTIMES. No skill runs
# Codex CLI, so only this catalogue names it.
VERIFY_TOOLS = {tool.name: tool for tool in (CODEX, COPILOT)}


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

    @property
    def outdated(self) -> bool:
        return bool(self.tool.minimum) and self.version is not None and self.version < self.tool.minimum


def probe(tool: Tool) -> Probe:
    """Locate a tool and, when it reports one, read its version."""
    path = tool.locate()
    if path is None or not tool.version_arguments:
        return Probe(tool, path, None)
    result = platform_support.run_tool([path, *tool.version_arguments])
    return Probe(tool, path, parse_version(result.output) if result.returncode == 0 else None)


def python_version() -> tuple[int, ...]:
    return tuple(sys.version_info[:3])
