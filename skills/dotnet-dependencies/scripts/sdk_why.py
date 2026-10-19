"""Run `dotnet nuget why` for the projects the SDK can answer, and read its tree back into paths.

The command is run once per project, with the project and one package, and prints a block: a heading naming the
project by its file stem, then for each target framework a tree of the dependency chains that reach the package,
drawn with box-drawing branches and the package highlighted with a terminal color. The heading wraps at the console
width even when the output is a pipe, the tree lines are padded to it instead, and the command exits 0 whether it
found the package, found no dependency on it, or found no assets file for a project, so the text alone says which.
That text is in the CLI's language, which follows the machine's display language unless `DOTNET_CLI_UI_LANGUAGE` or
`VSLANG` names another, so the command runs with `DOTNET_CLI_UI_LANGUAGE` set to English (`CLI_LANGUAGE`) and every
other variable kept, and prints the English the parser reads on every machine. The parser strips the colors, reads
a chain's depth from the column of its branch, and keeps every chain that ends at the package; a project with no
heading in the output, or whose run exited nonzero after printing one, was not answered, and the caller walks its own
graph, a run with no heading naming the first line it printed, which the parser could not read. The answer is keyed by
the project path the run was for, never by the heading's stem, which two projects may share.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

from bounded_process import non_interactive_environment, run_bounded
from executable_locator import find_executable, located_command

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
HEADING = re.compile(
    r"^Project '(?P<name>.+?)' (?P<state>has the following dependency graph|does not have a dependency)"
)
FRAMEWORK = re.compile(r"^\s*\[(?P<framework>[^\]]+)\]\s*$")
NODE = re.compile(r"^(?P<indent>[\s│]*)[├└]──\s+(?P<name>.+?)\s+\(v(?P<version>[^)]+)\)\s*$")
NO_ASSETS = "No assets file was found"
# The CLI language the command runs in, whatever the machine's display language or the variables naming another, so
# its text is the English HEADING and NO_ASSETS match. The invariant globalization setting is not added: with it the
# CLI refuses any language a variable names, printing an error line, and falls back to its own default.
CLI_LANGUAGE = {"DOTNET_CLI_UI_LANGUAGE": "en"}
# The column of the first branch, and the columns each deeper level adds.
TREE_BASE = 2
TREE_STEP = 4
DEFAULT_TIMEOUT_SECONDS = 120

Runner = Callable[[Sequence[str], float], "Completed"]


@dataclass(frozen=True)
class Completed:
    """What a `dotnet nuget why` run left: its exit code and its output, both streams together."""

    returncode: int
    text: str
    timed_out: bool = False


# The function that starts a command whose program tests/run_validation.py cannot read here, with the reason.
COMMANDS_ALLOWED = {
    "subprocess_runner": "the default runner of Services, which runs the dotnet command why_command() builds; the"
    " policy reads the program there",
}


def subprocess_runner(arguments: Sequence[str], timeout: float) -> Completed:
    """Run the command with no stdin, every prompt off, and the CLI in English (`CLI_LANGUAGE`), at the path
    ejryan89-skill-core's locator finds for its program."""
    try:
        result = run_bounded(located_command(arguments), timeout, env={**non_interactive_environment(), **CLI_LANGUAGE})
    except subprocess.TimeoutExpired as expired:
        output = expired.output if isinstance(expired.output, bytes) else b""
        return Completed(-1, output.decode("utf-8", errors="replace"), timed_out=True)
    return Completed(result.returncode, (result.stdout + result.stderr).decode("utf-8", errors="replace"))


@dataclass
class Services:
    """External effects, replaceable in tests."""

    run: Runner = subprocess_runner
    which: Callable[[str], str | None] = field(default=find_executable)


@dataclass(frozen=True)
class SdkPath:
    """One chain the SDK printed: the project's stem, the framework, and each hop's name and version."""

    project: str
    framework: str
    hops: tuple[tuple[str, str], ...]


@dataclass
class WhyAnswer:
    """What the runs answered, per project path: the chains, the projects answered, and why each failed run failed."""

    paths: dict[Path, list[SdkPath]] = field(default_factory=dict)
    answered: set[Path] = field(default_factory=set)
    failures: dict[Path, str] = field(default_factory=dict)


@dataclass
class ParsedWhy:
    """One run's output read back: the chains per heading's project stem, and the stems it answered."""

    paths: dict[str, list[SdkPath]] = field(default_factory=dict)
    answered: set[str] = field(default_factory=set)


def why_command(project: Path, package: str) -> list[str]:
    return ["dotnet", "nuget", "why", str(project), package]


def parse_why(text: str, package: str) -> ParsedWhy:
    """The chains to `package` in a run's output, per project stem, and the stems the run answered."""
    answer = ParsedWhy()
    target = package.casefold()
    project: str | None = None
    framework = ""
    stack: list[tuple[str, str]] = []
    for raw in ANSI.sub("", text).splitlines():
        line = raw.rstrip()
        heading = HEADING.match(line)
        if heading:
            project = heading.group("name")
            answer.answered.add(project)
            answer.paths.setdefault(project, [])
            framework, stack = "", []
            continue
        if project is None:
            continue
        bracket = FRAMEWORK.match(line)
        if bracket:
            framework, stack = bracket.group("framework"), []
            continue
        node = NODE.match(line)
        if not node:
            continue
        depth = max(0, (len(node.group("indent")) - TREE_BASE) // TREE_STEP)
        del stack[depth:]
        stack.append((node.group("name"), node.group("version")))
        if node.group("name").casefold() == target:
            answer.paths[project].append(SdkPath(project, framework, tuple(stack)))
    return answer


def first_line(text: str) -> str:
    """The first line of a run's output that holds anything but spaces, without its colors, or `""`."""
    return next((line.strip() for line in ANSI.sub("", text).splitlines() if line.strip()), "")


def run_why(projects: Sequence[Path], package: str, services: Services, timeout: float) -> WhyAnswer:
    """Ask the SDK why each project depends on `package`, one run per project: the command's usage shows several
    projects, but it refuses a second path as an unrecognized argument.

    A run that times out, cannot start, exits nonzero, or answers nothing records the project's failure and leaves
    that project to the graph walk, whatever part of an answer it printed first; the other projects keep theirs.
    """
    answer = WhyAnswer()
    for project in projects:
        try:
            result = services.run(why_command(project, package), timeout)
        except OSError as error:
            answer.failures[project] = f"dotnet nuget why could not start: {error}"
            continue
        part = parse_why(result.text, package)
        if result.timed_out:
            answer.failures[project] = f"dotnet nuget why did not finish within {timeout:g} seconds"
        elif part.answered and result.returncode != 0:
            answer.failures[project] = (
                f"dotnet nuget why exited with code {result.returncode} after printing part of an answer"
            )
        elif NO_ASSETS in result.text and not part.answered:
            answer.failures[project] = "dotnet nuget why answered nothing (no restored assets file)"
        elif not part.answered:
            first = first_line(result.text)
            unread = f"; the first line it printed, which the script cannot read: {first}" if first else ""
            answer.failures[project] = f"dotnet nuget why answered nothing (exit code {result.returncode}){unread}"
        else:
            answer.answered.add(project)
            answer.paths[project] = [chain for chains in part.paths.values() for chain in chains]
    return answer
