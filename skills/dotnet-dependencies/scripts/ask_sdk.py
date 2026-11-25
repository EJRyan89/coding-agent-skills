"""Answer `why` as dependency_graph.py does, and ask `dotnet nuget why` to locate the restore of each project whose
assets file the walk cannot place, the one route on which the skill starts the .NET SDK.

    ask_sdk.py <name> [<name> ...] [--root PATH] [--project PATH]

It prints the lines dependency_graph.py's docstring names for `why`, with an `sdk` source for the chains the SDK
printed and an SDK_FAILED line for a run that failed. `dotnet nuget why` evaluates each such project through MSBuild,
and MSBuild and the .NET host that starts it read and run whatever the project and the machine's SDK configuration
lead them to, beyond the examined tree, an SDK a global.json's sdk.paths names included. So the skill grants this
script to no runtime: Claude Code asks the user before each run, as SKILL.md has the agent ask first in chat. The
command runs once per project and name, in the project's folder, so the .NET host picks the SDK by the nearest
global.json at or above it, with no stdin, every prompt off, and the CLI in English (`CLI_LANGUAGE`), whatever the
machine's display language or `DOTNET_CLI_UI_LANGUAGE` or `VSLANG` names, so its text is the English sdk_why reads.

Exit status:
  0  the query ran and printed no CONFLICT, REDIRECT_STALE, or UNREADABLE line
  1  a CONFLICT, REDIRECT_STALE, or UNREADABLE line, or a last line FAILED <reason>
  2  a usage error
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import dependency_graph
from bounded_process import non_interactive_environment, run_bounded
from console import run_main
from executable_locator import located_command
from sdk_why import Completed, Services

# The SDK this starts may be one the target repository names (global.json's sdk.paths), and MSBuild evaluates the
# repository's own project files and the files they lead to, so the skill leaves this script ungranted and the user
# approves each run.
RUNS_REPOSITORY_CODE = (
    "starts dotnet nuget why, whose .NET host and MSBuild read and run what the target repository's global.json and "
    "project files name"
)
# The CLI language the command runs in, whatever the machine's display language or the variables naming another, so
# its text is the English sdk_why's HEADING and NO_ASSETS match. The invariant globalization setting is not added: with
# it the CLI refuses any language a variable names, printing an error line, and falls back to its own default.
CLI_LANGUAGE = {"DOTNET_CLI_UI_LANGUAGE": "en"}
# The function that starts a command whose program tests/run_validation.py cannot read here, with the reason.
COMMANDS_ALLOWED = {
    "subprocess_runner": "the runner of this entry point's Services, which runs the dotnet command sdk_why's "
    "why_command() builds; the policy reads the program there",
}


def subprocess_runner(arguments: Sequence[str], timeout: float, folder: Path) -> Completed:
    """Run the command in `folder` with no stdin, every prompt off, and the CLI in English (`CLI_LANGUAGE`), at the path
    ejryan89-skill-core's locator finds for its program."""
    environment = {**non_interactive_environment(), **CLI_LANGUAGE}
    try:
        result = run_bounded(located_command(arguments), timeout, cwd=folder, env=environment)
    except subprocess.TimeoutExpired as expired:
        output = expired.output if isinstance(expired.output, bytes) else b""
        return Completed(-1, output.decode("utf-8", errors="replace"), timed_out=True)
    return Completed(result.returncode, (result.stdout + result.stderr).decode("utf-8", errors="replace"))


def main(argv: Sequence[str] | None = None, services: Services | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else list(argv)
    return dependency_graph.main(["why", *arguments], services or Services(run=subprocess_runner), ask_sdk=True)


if __name__ == "__main__":
    run_main(main, errors="replace")
