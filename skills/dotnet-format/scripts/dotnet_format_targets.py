"""Resolve the changed C# files and the solution the dotnet-format skill formats.

    resolve [--cwd PATH]   print what to format; PATH is the directory the skill was invoked from, by default the
                           current directory

Output, one fact per line, tab separated:

    REPO_ROOT       <absolute repository root>
    FETCH_FAILED    <reason>                      `git fetch origin` failed; BASE is origin as last fetched
    BASE            <base ref the branch is compared with>
    SKIPPED_ASPNET  <file> <owning .csproj>       file belongs to an ASP.NET project, which crashes the formatter
    FILE            <file>                        a changed C# file to format
    OUTSIDE_SOLUTION <file>                       a FILE no project of SOLUTION owns; the formatter skips it
    FILE_LIST       <temporary file>              the FILE paths, one per line, for --include-file / --file-list
    SOLUTION        <solution> <score>            the solution to format and how many FILEs its projects own
    STOP            <reason>                      nothing to format; report the reason and stop

BASE is the pull request's base branch when gh reports one, else the remote's default branch (origin/HEAD), else
origin/main, else origin/master, after `git fetch origin` brings them up to date. SOLUTION is the nearest solution
(.sln or .slnx) from the invocation directory that owns a FILE, else the repository solution owning the most. A
solution owning none of them is never chosen, because the formatter would skip every file and report a clean result;
that ends in STOP, as does finding no changed C# file or no solution, or choosing a .slnx solution when the .NET
SDK's dotnet, which alone formats one, is not installed.

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
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

from console import use_utf8_output
from git_client import GitClient, GitError, GitResult, classify_failure
from github_client import GitHubClient, GitHubError, subprocess_runner
from github_client import Runner as GhRunner

WEB_PROJECT = re.compile(
    r"Microsoft\.NET\.Sdk\.Web|<WebApplication>|<WebSiteType>|349c5851-65df-11da-9384-00065b846f21",
    re.IGNORECASE,
)
SOLUTION_PROJECT = re.compile(r'^\s*Project\("[^"]*"\)\s*=\s*"[^"]*"\s*,\s*"([^"]+)"')
IGNORED_DIRECTORIES = frozenset({".git", "bin", "obj", "node_modules"})
REMOTE_REFS = "refs/remotes/origin/"
SOLUTION_SUFFIXES = (".sln", ".slnx")
XML_DECLARATION = re.compile(r"\A\s*<\?xml[^>]*\?>")


def gh_runner_in(directory: Path) -> GhRunner:
    """A gh runner working in `directory`, since `gh pr view` reads the repository and branch from there."""
    return partial(subprocess_runner, cwd=directory)


@dataclass
class Services:
    """External effects, replaceable in tests."""

    git: GitClient = field(default_factory=GitClient)
    gh: Callable[[Path], GhRunner] = gh_runner_in
    which: Callable[[str], str | None] = field(default=shutil.which)


class Stop(Exception):
    """There is nothing to format; the message says why."""


class Failed(Exception):
    """The targets could not be resolved; the message says why."""


def read_text(path: Path) -> str:
    """Read an MSBuild or solution file whatever its encoding; only ASCII syntax matters here."""
    data = path.read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    return data.decode("utf-8-sig", errors="replace")


def same_path(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def git(services: Services, directory: Path, *arguments: str) -> GitResult:
    """Run git in directory; git that is missing, cannot start, or does not finish fails the resolution."""
    try:
        return services.git.run(arguments, directory=directory)
    except GitError as exc:
        raise Failed(str(exc)) from exc


def one_line(text: str) -> str:
    """Text git printed, as one line for a tab-separated output line."""
    return " ".join(text.split())


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
        raise Failed(one_line(result.stderr) or f"git rev-parse --show-toplevel exited with code {result.returncode}")
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
        return one_line(str(exc))
    if result.returncode == 0:
        return None
    return one_line(result.stderr) or f"git fetch origin exited with code {result.returncode}"


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
    directory = file.parent
    while True:
        projects = sorted(path for path in directory.glob("*.csproj") if path.is_file())
        if projects:
            return projects
        if same_path(directory) == same_path(root) or directory.parent == directory:
            return []
        directory = directory.parent


def is_web_project(project: Path) -> bool:
    return bool(WEB_PROJECT.search(read_text(project)))


def is_solution(path: Path) -> bool:
    return path.name.casefold().endswith(SOLUTION_SUFFIXES)


def listed_projects(solution: Path) -> list[str]:
    """The project paths a solution lists, as written: `Project(...) = "name", "path"` lines in a .sln, and the Path
    of each Project element in a .slnx. A .slnx that is not well-formed XML, or declares a DOCTYPE or an entity,
    lists none, as a .sln line that does not match lists nothing."""
    text = read_text(solution)
    if solution.name.casefold().endswith(".slnx"):
        # Declarations can expand entities without bound; no solution needs them.
        if "<!DOCTYPE" in text or "<!ENTITY" in text:
            return []
        try:
            # The text is already decoded, so a declaration naming another encoding would only contradict it.
            document = ElementTree.fromstring(  # noqa: S314 - DOCTYPE and ENTITY are refused above, so nothing expands
                XML_DECLARATION.sub("", text, count=1)
            )
        except (ElementTree.ParseError, ValueError):
            return []
        return [path for element in document.iter("Project") if (path := element.get("Path"))]
    return [match.group(1) for line in text.splitlines() if (match := SOLUTION_PROJECT.match(line))]


def solution_projects(solution: Path) -> set[str]:
    """Normalized absolute paths of the projects a solution lists."""
    return {same_path(solution.parent / path.replace("\\", "/")) for path in listed_projects(solution)}


def nearest_solutions(root: Path, cwd: Path) -> list[Path]:
    """Solutions in the nearest directory from cwd up to the root, or none."""
    inside = same_path(cwd) == same_path(root) or any(same_path(parent) == same_path(root) for parent in cwd.parents)
    directory = cwd if inside else root
    while True:
        found = sorted(path for path in directory.iterdir() if is_solution(path) and path.is_file())
        if found:
            return found
        if same_path(directory) == same_path(root) or directory.parent == directory:
            return []
        directory = directory.parent


def raise_error(error: OSError) -> None:
    raise error


def find_solutions(root: Path) -> list[Path]:
    """Every solution in the repository, skipping build and dependency directories.

    A directory it cannot read is an error, not an empty one: skipping it could hide the solution to choose.
    """
    found = []
    for current, directories, files in os.walk(root, onerror=raise_error):
        directories[:] = sorted(name for name in directories if name.casefold() not in IGNORED_DIRECTORIES)
        found += [Path(current) / name for name in sorted(files) if is_solution(Path(name))]
    return found


def owns(solution: Path, owned: list[Path]) -> bool:
    projects = solution_projects(solution)
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
    kept: list[str] = []
    owners: list[list[Path]] = []
    for name in changed_files(services, root, base):
        projects = owning_projects(root, root / name)
        web = next((project for project in projects if is_web_project(project)), None)
        if web is not None:
            emit(f"SKIPPED_ASPNET\t{name}\t{relative(web, root)}")
            continue
        kept.append(name)
        owners.append(projects)
    if not kept:
        raise Stop(f"no C# files changed vs {base} (excluding ASP.NET projects)")
    for name in kept:
        emit(f"FILE\t{name}")
    solution, score = choose_solution(root, cwd.resolve(), owners)
    for name, owned in zip(kept, owners, strict=True):
        if not owns(solution, owned):
            emit(f"OUTSIDE_SOLUTION\t{name}")
    if solution.name.casefold().endswith(".slnx") and not services.which("dotnet"):
        raise Stop(
            f"{relative(solution, root)} is a .slnx solution, which only the .NET SDK's dotnet format opens; "
            "install the .NET SDK 9.0.200 or newer"
        )
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
    use_utf8_output(errors="replace")
    sys.exit(main())
