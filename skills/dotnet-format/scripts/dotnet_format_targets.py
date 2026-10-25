"""Resolve the changed C# files and the solution the dotnet-format skill formats.

    resolve [--cwd PATH]   print what to format; PATH is the directory the skill was invoked from, by default the
                           current directory

Output, one fact per line, tab separated:

    REPO_ROOT       <absolute repository root>
    FETCH_FAILED    <reason>                      `git fetch origin` failed; BASE is origin as last fetched
    BASE            <base ref the branch is compared with>
    FILE            <file>                        a changed C# file to format
    OUTSIDE_SOLUTION <file>                       a FILE no project of SOLUTION owns; the formatter skips it
    SKIPPED_ASPNET  <file> <owning .csproj>       a FILE of an ASP.NET project, which the global tool is not run on
    FILE_LIST       <temporary file>              the FILE paths, one per line, for --include-file / --file-list
    SOLUTION        <solution> <score>            the solution to format and how many FILEs its projects own
    STOP            <reason>                      nothing to format; report the reason and stop

BASE is the pull request's base branch when gh reports one, else the remote's default branch (origin/HEAD), else
origin/main, else origin/master, after `git fetch origin` brings them up to date. SOLUTION is the nearest solution
(.sln or .slnx) from the invocation directory that owns a FILE, else the repository solution owning the most. A
solution owning none of them is never chosen, because the formatter would skip every file and report a clean result;
that ends in STOP, as does finding no changed C# file or no solution, or choosing a solution the .NET SDK's
`dotnet format` formats when dotnet is not installed.

The formatter is the .NET SDK's `dotnet format` for a .slnx solution, which the dotnet-format global tool cannot open,
and for a .sln whose C# and Visual Basic projects are all SDK-style, since the global tool cannot load the analyzers
current SDKs give such projects; it is the global tool for any other .sln, such as one holding a .NET Framework project
in the older format, on which the SDK's command can fail. The global tool is not run on an ASP.NET project's files,
which SKIPPED_ASPNET names: the SDK's command formats them, so the skip holds only for the global tool. A FILE that
SKIPPED_ASPNET or OUTSIDE_SOLUTION names still goes into FILE_LIST, for the layout checker.

FILE, SKIPPED_ASPNET, and SOLUTION paths are relative to REPO_ROOT with forward slashes, because dotnet-format
runs from REPO_ROOT and matches --include paths against that directory.

Exits 0 with its lines, ending in SOLUTION or STOP. When the directory is not in a Git repository, git is missing,
a git command fails or does not finish in time, no base ref exists, or a file cannot be read or written, it prints
`FAILED <reason>` as the last line and exits 1. A usage error exits 2.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

import msbuild_xml
from console import run_main
from dotnet_solution import (
    SEARCH_SKIPPED_FOLDERS,
    is_solution,
    lexical_absolute,
    project_path,
    read_listed_projects,
    read_text,
    refusal,
    same_path,
    solution_projects,
)
from executable_locator import find_executable
from flat_text import flat_text
from folder_walk import LINKED, LOOP, NESTED_CHECKOUT, walk_files
from folders_up import folders_up
from git_client import GitClient, GitError, GitResult, classify_failure
from github_client import GitHubClient, GitHubError, subprocess_runner
from github_client import Runner as GhRunner
from xml_reader import XmlRefused, read_xml

# An ASP.NET project, whose files the dotnet-format global tool is not run on. The skill has skipped them since its
# first version as crashing the formatter, with no record of which project kind did; the SDK's dotnet format formats
# them, so `formatter_skips` holds the skip to the global tool alone.
WEB_PROJECT = re.compile(
    r"Microsoft\.NET\.Sdk\.Web|<WebApplication>|<WebSiteType>|349c5851-65df-11da-9384-00065b846f21",
    re.IGNORECASE,
)
# The project files the formatters format, whose style chooses between them; any other, such as a C++ project, is left
# out of the choice.
FORMATTED_PROJECT_SUFFIXES = (".csproj", ".vbproj")
SDK_FORMATTER = "dotnet format"
TOOL_FORMATTER = "dotnet-format"
REMOTE_REFS = "refs/remotes/origin/"
# Why a solution needs the .NET SDK's dotnet, after its name.
SDK_SOLUTION = "is a .slnx solution, or a .sln of SDK-style projects, which the .NET SDK's dotnet format formats"


def gh_runner_in(directory: Path) -> GhRunner:
    """A gh runner working in `directory`, since `gh pr view` reads the repository and branch from there."""
    return partial(subprocess_runner, cwd=directory)


@dataclass
class Services:
    """External effects, replaceable in tests."""

    git: GitClient = field(default_factory=GitClient)
    gh: Callable[[Path], GhRunner] = gh_runner_in
    # ejryan89-skill-core's locator, which finds a tool where `deploy.py check` does.
    which: Callable[[str], str | None] = field(default=find_executable)


class Stop(Exception):
    """There is nothing to format; the message says why."""


class Failed(Exception):
    """The targets could not be resolved; the message says why."""


def read_file_list(path: Path) -> list[str]:
    """The paths a FILE_LIST names, one per line, without blank lines or a byte order mark.

    Raises OSError when it cannot be read, and ValueError when it is not UTF-8, names no file, or holds a NUL on a
    line, which no path holds, naming that line: resolve writes a FILE_LIST only when it found files to format, so an
    empty one did not come from it.
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError(f"{path} is not UTF-8 text") from None
    for number, line in enumerate(text.splitlines(), start=1):
        if "\0" in line:
            raise ValueError(f"{path} line {number} holds a NUL, which no path holds")
    files = [name for name in (line.strip() for line in text.splitlines()) if name]
    if not files:
        raise ValueError(f"{path} lists no files")
    return files


def listed_refusal(root: Path, name: str) -> str | None:
    """Why a script does not read or write the file a FILE_LIST entry names, or None when it lies inside `root`.

    ejryan89-skill-core's `refusal` judges the entry first as written, before anything touches it, so an absolute
    entry, one whose `..` segments leave the root, and a network-share or device form are refused unopened; then as
    resolved, so an entry inside the root that a symbolic link or junction leads out of it is refused too."""
    base = lexical_absolute(root)
    path = project_path(base, name)
    refused = refusal(path, base)
    if refused:
        return refused
    try:
        leaves = refusal(path.resolve(), base.resolve()) is not None
    except OSError as error:
        return f"the path cannot be resolved: {error.strerror or error}"
    if leaves:
        return "a symbolic link or junction leads it outside the repository root, which the script does not leave"
    return None


def solution_refusal(root: Path, solution: str) -> str | None:
    """Why a script does not read or format the solution a `--solution` value names, judged as `listed_refusal`
    judges a FILE_LIST entry, or None when it lies inside `root`."""
    refused = listed_refusal(root, solution)
    return f"the solution {solution} is refused: {refused}" if refused else None


def inside_root(root: Path, names: list[str], emit: Callable[[str], None]) -> list[str]:
    """The FILE_LIST entries `listed_refusal` keeps, in order; each other entry prints `REFUSED <entry> <reason>`."""
    kept: list[str] = []
    for name in names:
        refused = listed_refusal(root, name)
        if refused:
            emit(f"REFUSED\t{name}\t{refused}")
        else:
            kept.append(name)
    return kept


def relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def git(services: Services, directory: Path, *arguments: str) -> GitResult:
    """Run git in directory; git that is missing, cannot start, or does not finish fails the resolution."""
    try:
        return services.git.run(arguments, directory=directory)
    except GitError as exc:
        raise Failed(str(exc)) from exc


def git_paths(services: Services, root: Path, *arguments: str) -> list[str]:
    result = git(services, root, arguments[0], "-z", *arguments[1:])
    if result.returncode != 0:
        raise Failed(f"git {' '.join(arguments)} failed")
    return [name for name in result.stdout.split("\0") if name]


def repository_root(services: Services, cwd: Path) -> Path:
    result = git(services, cwd, "rev-parse", "--show-toplevel")
    if result.returncode != 0:
        if classify_failure(result.stderr) == "not_repository":
            raise Failed(f"{cwd} is not inside a Git repository")
        raise Failed(flat_text(result.stderr) or f"git rev-parse --show-toplevel exited with code {result.returncode}")
    return Path(result.stdout.strip()).resolve()


def ref_exists(services: Services, root: Path, ref: str) -> bool:
    return git(services, root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").returncode == 0


def pull_request_base(services: Services, root: Path) -> str | None:
    """The base branch `gh pr view --json baseRefName` reports, or None when gh does not report one.

    Any gh failure gives None, and a rate limit is not waited out: the base is a hint with fallbacks after it.
    """
    try:
        output = GitHubClient(services.gh(root)).run(["pr", "view", "--json", "baseRefName"], retry=False)
        document = json.loads(output.stdout)
    except (GitHubError, ValueError):
        return None
    name = document.get("baseRefName") if isinstance(document, dict) else None
    if not isinstance(name, str) or not name.strip():
        return None
    return name.strip()


def remote_default_branch(result: GitResult) -> str | None:
    """origin/<name> from `git symbolic-ref refs/remotes/origin/HEAD` output, or None when origin has no HEAD."""
    if result.returncode != 0:
        return None
    target = result.stdout.strip()
    name = target.removeprefix(REMOTE_REFS)
    return f"origin/{name}" if name and name != target else None


def fetch_failure(services: Services, root: Path) -> str | None:
    """Why `git fetch origin` failed, or None when it succeeded.

    A failed fetch, offline or a remote that does not answer, leaves the remote-tracking refs as they were, which
    still serve as a base, so the caller reports it and goes on.
    """
    try:
        result = services.git.run(["fetch", "origin", "--quiet"], directory=root)
    except GitError as exc:
        return flat_text(str(exc))
    if result.returncode == 0:
        return None
    return flat_text(result.stderr) or f"git fetch origin exited with code {result.returncode}"


def base_ref(services: Services, root: Path, emit: Callable[[str], None]) -> str:
    """The first that exists of the pull request's base branch when gh knows it, the remote's default branch
    (origin/HEAD, which clone sets), origin/main, and origin/master, after fetching origin."""
    failure = fetch_failure(services, root)
    if failure:
        emit(f"FETCH_FAILED\t{failure}")
    candidates: list[str] = []
    if services.which("gh"):
        name = pull_request_base(services, root)
        if name:
            candidates.append(f"origin/{name}")
    default = remote_default_branch(git(services, root, "symbolic-ref", "--quiet", f"{REMOTE_REFS}HEAD"))
    if default:
        candidates.append(default)
    candidates += ["origin/main", "origin/master"]
    for candidate in candidates:
        if ref_exists(services, root, candidate):
            return candidate
    raise Failed("no base ref: none of the pull request base, origin/HEAD, origin/main, or origin/master exists")


def changed_files(services: Services, root: Path, base: str) -> list[str]:
    """Committed, uncommitted, and untracked C# files changed on this branch that still exist."""
    names = set(git_paths(services, root, "diff", "--name-only", "--diff-filter=AMR", f"{base}...HEAD", "--", "*.cs"))
    names |= set(git_paths(services, root, "diff", "--name-only", "HEAD", "--", "*.cs"))
    names |= set(git_paths(services, root, "ls-files", "--others", "--exclude-standard", "--", "*.cs"))
    return sorted(name for name in names if (root / name).is_file())


def owning_projects(root: Path, file: Path) -> list[Path]:
    """The .csproj files in the nearest directory at or above the file, up to and including the root."""
    for folder in folders_up(file.parent, root):
        projects = sorted(path for path in folder.glob("*.csproj") if path.is_file())
        if projects:
            return projects
    return []


def is_web_project(project: Path) -> bool:
    return bool(WEB_PROJECT.search(read_text(project)))


def is_sdk_style(project: Path) -> bool:
    """Whether a project file is SDK-style, as ejryan89-skill-core's `msbuild_xml` reads one: its Project element names
    an Sdk, or it holds an Sdk element or an Import of an Sdk. A file that cannot be read is not shown to be one."""
    try:
        return msbuild_xml.is_sdk_style(read_xml(read_text(project)))
    except (OSError, XmlRefused):
        return False


def formatter_for(root: Path, solution: Path) -> str:
    """The formatter for a solution: the SDK's `dotnet format` for a .slnx, and for a .sln listing C# or Visual Basic
    projects that are all SDK-style files inside `root`; the dotnet-format global tool for any other .sln, one it
    cannot read included, so that only a solution shown to suit the SDK's command gets it. A relative root or
    solution is taken from the working directory, as the formatter takes them."""
    if solution.name.casefold().endswith(".slnx"):
        return SDK_FORMATTER
    root, solution = lexical_absolute(root), lexical_absolute(solution)
    try:
        listed = read_listed_projects(solution)
    except (OSError, XmlRefused):
        return TOOL_FORMATTER
    projects = [
        project
        for entry in listed
        if (project := project_path(solution.parent, entry)).suffix.casefold() in FORMATTED_PROJECT_SUFFIXES
    ]
    if projects and all(refusal(project, root) is None and is_sdk_style(project) for project in projects):
        return SDK_FORMATTER
    return TOOL_FORMATTER


def web_project(root: Path, name: str) -> Path | None:
    """The ASP.NET project owning a file, of those `owning_projects` gives, or None."""
    return next((project for project in owning_projects(root, root / name) if is_web_project(project)), None)


def formatter_skips(root: Path, formatter: str, names: list[str]) -> dict[str, Path]:
    """The files `formatter` is not run on, each with the ASP.NET project owning it: none for the SDK's command,
    which formats them, and every ASP.NET project's file for the global tool."""
    if formatter != TOOL_FORMATTER:
        return {}
    return {name: project for name in names if (project := web_project(root, name)) is not None}


def nearest_solutions(root: Path, cwd: Path) -> list[Path]:
    """Solutions in the nearest directory from cwd up to the root, or none."""
    inside = any(same_path(folder) == same_path(root) for folder in folders_up(cwd))
    for folder in folders_up(cwd if inside else root, root):
        found = sorted(path for path in folder.iterdir() if is_solution(path) and path.is_file())
        if found:
            return found
    return []


def find_solutions(root: Path) -> list[Path]:
    """Every solution in the repository, in ejryan89-skill-core's folder walk's order, outside the folders the .NET
    search skips (`dotnet_solution.SEARCH_SKIPPED_FOLDERS`) and nested checkouts, never through a junction or symbolic
    link.

    A folder it cannot list, or an entry it cannot examine, is an error, not an empty one: skipping it could hide the
    solution to choose. A linked folder is not one, since the link may lead out of the repository or back into it.
    """
    walk = walk_files(root, lambda name: is_solution(Path(name)), skipped=SEARCH_SKIPPED_FOLDERS)
    for entry in walk.not_entered:
        if entry.kind not in (LINKED, LOOP, NESTED_CHECKOUT):
            raise Failed(f"{entry.path}: {entry.reason}")
    return walk.files


def owns(solution: Path, owned: list[Path]) -> bool:
    """Whether the solution lists a project that owns the file; a solution that cannot be read fails, since taking
    it for one that lists no project would name the wrong cause."""
    try:
        projects = solution_projects(solution)
    except XmlRefused as error:
        raise Failed(f"{solution} cannot be read: {error}") from None
    return any(same_path(project) in projects for project in owned)


def best_solution(root: Path, candidates: list[Path], owners: list[list[Path]]) -> tuple[Path, int]:
    """The solution owning the most changed files; ties go to the shallowest, then to a .sln over a .slnx (the
    dotnet-format global tool opens only a .sln), then by name."""
    ranked = sorted(
        ((sum(1 for owned in owners if owns(solution, owned)), solution) for solution in candidates),
        key=lambda item: (
            -item[0],
            len(item[1].relative_to(root).parts),
            item[1].name.casefold().endswith(".slnx"),
            relative(item[1], root).casefold(),
        ),
    )
    best_score, best = ranked[0]
    return best, best_score


def choose_solution(root: Path, cwd: Path, owners: list[list[Path]]) -> tuple[Path, int]:
    """The nearest solution that owns a changed file, else the repository solution owning the most.

    A solution that owns none of the changed files is never chosen: the formatter would skip every file and
    report a clean result.
    """
    nearest = nearest_solutions(root, cwd)
    if nearest:
        best, score = best_solution(root, nearest, owners)
        if score:
            return best, score
    everything = find_solutions(root)
    if not everything:
        raise Stop(f"no .sln or .slnx found under {root}")
    best, score = best_solution(root, everything, owners)
    if not score:
        names = ", ".join(relative(solution, root) for solution in everything)
        raise Stop(f"changed files do not belong to any solution found: {names}")
    return best, score


def resolve(cwd: Path, services: Services, emit: Callable[[str], None]) -> None:
    root = repository_root(services, cwd)
    emit(f"REPO_ROOT\t{root}")
    base = base_ref(services, root, emit)
    emit(f"BASE\t{base}")
    kept = changed_files(services, root, base)
    if not kept:
        raise Stop(f"no C# files changed vs {base}")
    owners = [owning_projects(root, root / name) for name in kept]
    for name in kept:
        emit(f"FILE\t{name}")
    solution, score = choose_solution(root, cwd.resolve(), owners)
    formatter = formatter_for(root, solution)
    skipped = formatter_skips(root, formatter, kept)
    for name, owned in zip(kept, owners, strict=True):
        if not owns(solution, owned):
            emit(f"OUTSIDE_SOLUTION\t{name}")
        elif name in skipped:
            emit(f"SKIPPED_ASPNET\t{name}\t{relative(skipped[name], root)}")
    if formatter == SDK_FORMATTER and not services.which("dotnet"):
        raise Stop(f"{relative(solution, root)} {SDK_SOLUTION}; install the .NET SDK 9.0.200 or newer")
    descriptor, list_path = tempfile.mkstemp(prefix="dotnet-format-files-", suffix=".txt")
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("".join(f"{name}\n" for name in kept))
    emit(f"FILE_LIST\t{list_path}")
    emit(f"SOLUTION\t{relative(solution, root)}\t{score}")


def main(argv: Sequence[str] | None = None, services: Services | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    resolve_parser = commands.add_parser("resolve", help="resolve the changed files and the solution")
    resolve_parser.add_argument(
        "--cwd", type=Path, help="directory the skill was invoked from (default: the current directory)"
    )
    arguments = parser.parse_args(argv)
    try:
        resolve(arguments.cwd or Path.cwd(), services or Services(), print)
    except Stop as stop:
        print(f"STOP\t{stop}")
    except (Failed, OSError) as error:
        print(f"FAILED {error}")
        return 1
    return 0


if __name__ == "__main__":
    run_main(main, errors="replace")
