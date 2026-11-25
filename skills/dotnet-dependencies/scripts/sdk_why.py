"""Run `dotnet nuget why` for the projects the SDK can answer, and read its tree back into paths.

The command is run once per project, with the project and one package, in the project's folder, by the runner the caller
passes (ask_sdk.py's, the only one that starts the SDK; `no_sdk` otherwise), so the .NET host picks the SDK by the
nearest global.json at or above the project, the one MSBuild's resolvers read, and prints a block: a heading naming the
project by its file stem, then for each target framework, and each runtime-specific target as `<framework>/<runtime>`, a
tree of the dependency chains that reach the package, drawn with box-drawing branches, each package with its version and
each referenced project by its name alone, frameworks whose trees are alike printed as consecutive lines over one tree,
and the package highlighted with a terminal color. The heading wraps at the console width even when the output is a
pipe, the tree lines are padded to it instead, and the command exits 0 whether it found the package, found no dependency
on it, or found no assets file for a project, so the text alone says which. That text is in the CLI's language, which
ask_sdk.py's runner sets to English, so the parser reads it on every machine. The parser strips the colors, reads a
chain's depth from the column of its branch, and keeps every chain that ends at the package; a project with no heading
in the output, or whose run exited nonzero after printing one, was not answered, and the caller walks its own graph, a
run with no heading naming the first line it printed, which the parser could not read. The answer is keyed by the
project path the run was for, never by the heading's stem, which two projects may share.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

from executable_locator import find_program
from flat_text import flat_text, reason_line
from ordinal_case import ordinal_fold
from text_lines import program_lines

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
HEADING = re.compile(
    r"^Project '(?P<name>.+?)' (?P<state>has the following dependency graph|does not have a dependency)"
)
FRAMEWORK = re.compile(r"^\s*\[(?P<framework>[^\]]+)\]\s*$")
# A tree line: a package with its version, or a referenced project by its name alone.
NODE = re.compile(r"^(?P<indent>[\s│]*)[├└]──\s+(?P<name>.+?)(?:\s+\(v(?P<version>[^)]+)\))?\s*$")
NO_ASSETS = "No assets file was found"
# The column of the first branch, and the columns each deeper level adds.
TREE_BASE = 2
TREE_STEP = 4
DEFAULT_TIMEOUT_SECONDS = 120

# A command, its time limit in seconds, and the folder it runs in.
Runner = Callable[[Sequence[str], float, Path], "Completed"]


@dataclass(frozen=True)
class Completed:
    """What a `dotnet nuget why` run left: its exit code and its output, both streams together."""

    returncode: int
    text: str
    timed_out: bool = False


def no_sdk(arguments: Sequence[str], timeout: float, folder: Path) -> Completed:
    """The runner of an entry point that may not start the SDK, dependency_graph.py's, which never asks it: only
    ask_sdk.py, which the skill grants to no runtime, runs `dotnet nuget why`."""
    raise OSError("this entry point starts no dotnet; ask_sdk.py does")


@dataclass
class Services:
    """External effects, replaceable in tests."""

    run: Runner = no_sdk
    which: Callable[[str], str | None] = field(default=find_program)


@dataclass(frozen=True)
class SdkPath:
    """One chain the SDK printed: the project's stem, the framework, and each hop's name and version, `""` for a
    referenced project, which the SDK prints by its name alone."""

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
    """One run's output read back: the chains per heading's project stem, the stems it answered, and the lines it
    printed after the last line of its answer, such as an error it states last."""

    paths: dict[str, list[SdkPath]] = field(default_factory=dict)
    answered: set[str] = field(default_factory=set)
    after: list[str] = field(default_factory=list)


def why_command(project: Path, package: str) -> list[str]:
    return ["dotnet", "nuget", "why", str(project), package]


def parse_why(text: str, package: str) -> ParsedWhy:
    """The chains to `package` in a run's output, per project stem, the stems the run answered, and the lines after
    the answer's last heading, framework, or tree line. A heading the CLI wraps at the console width goes on to the
    first blank line, so its wrapped part is never taken for a line after the answer. A line printed right after a
    heading, with no blank line between, is read as the heading's wrapped part, so an error there is not quoted and
    the failure names the exit code alone. Frameworks whose graphs are alike are printed as consecutive framework
    lines over one tree, so a tree holds for every framework line before it since the last tree line."""
    answer = ParsedWhy()
    target = ordinal_fold(package)
    project: str | None = None
    frameworks: list[str] = []
    grouping = False
    stack: list[tuple[str, str]] = []
    lines = program_lines(ANSI.sub("", text))
    end = 0
    in_heading = False
    for index, raw in enumerate(lines):
        line = raw.rstrip()
        heading = HEADING.match(line)
        if heading:
            project = heading.group("name")
            answer.answered.add(project)
            answer.paths.setdefault(project, [])
            frameworks, grouping, stack = [], False, []
            end, in_heading = index + 1, True
            continue
        if in_heading and line.strip() and not FRAMEWORK.match(line) and not NODE.match(line):
            end = index + 1
            continue
        in_heading = False
        if project is None:
            continue
        bracket = FRAMEWORK.match(line)
        if bracket:
            frameworks = [*frameworks, bracket.group("framework")] if grouping else [bracket.group("framework")]
            grouping, stack = True, []
            end = index + 1
            continue
        node = NODE.match(line)
        if not node:
            continue
        grouping = False
        end = index + 1
        depth = max(0, (len(node.group("indent")) - TREE_BASE) // TREE_STEP)
        del stack[depth:]
        stack.append((node.group("name"), node.group("version") or ""))
        if ordinal_fold(node.group("name")) == target:
            answer.paths[project] += [SdkPath(project, framework, tuple(stack)) for framework in frameworks or [""]]
    answer.after = lines[end:] if answer.answered else []
    return answer


def first_line(text: str) -> str:
    """The first line of a run's output that holds anything but spaces, without its colors, or `""`."""
    return next((line.strip() for line in program_lines(ANSI.sub("", text)) if line.strip()), "")


def run_why(projects: Sequence[Path], package: str, services: Services, timeout: float) -> WhyAnswer:
    """Ask the SDK why each project depends on `package`, one run per project, in the project's folder: the command's
    usage shows several projects, but it refuses a second path as an unrecognized argument.

    A run that times out, cannot start, exits nonzero, or answers nothing records the project's failure and leaves
    that project to the graph walk, whatever part of an answer it printed first; the other projects keep theirs.
    """
    answer = WhyAnswer()
    for project in projects:
        try:
            result = services.run(why_command(project, package), timeout, project.parent)
        except OSError as error:
            answer.failures[project] = f"dotnet nuget why could not start: {flat_text(str(error))}"
            continue
        part = parse_why(result.text, package)
        if result.timed_out:
            answer.failures[project] = f"dotnet nuget why did not finish within {timeout:g} seconds"
        elif part.answered and result.returncode != 0:
            # The CLI states an error after the part of an answer it printed, so the lines after that part are the
            # reason it gave; with none, the exit code says all there is.
            after = reason_line("\n".join(part.after))
            stated = f"dotnet nuget why exited with code {result.returncode} after printing part of an answer"
            answer.failures[project] = f"{stated}, then: {after}" if after else stated
        elif NO_ASSETS in result.text and not part.answered:
            answer.failures[project] = "dotnet nuget why answered nothing (no restored assets file)"
        elif not part.answered:
            first = reason_line(first_line(result.text))
            unread = f"; the first line it printed, which the script cannot read: {first}" if first else ""
            answer.failures[project] = f"dotnet nuget why answered nothing (exit code {result.returncode}){unread}"
        else:
            answer.answered.add(project)
            answer.paths[project] = [chain for chains in part.paths.values() for chain in chains]
    return answer
