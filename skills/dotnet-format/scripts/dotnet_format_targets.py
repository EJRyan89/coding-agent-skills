"""Resolve the changed C# files and the solution the dotnet-format skill formats.

    resolve [--cwd PATH]   print what to format; PATH is the directory the skill was invoked from, by default the
                           current directory

Output, one fact per line, tab separated:

    REPO_ROOT       <absolute repository root>
    BASE            <base ref the branch is compared with>
    SKIPPED_ASPNET  <file> <owning .csproj>       file belongs to an ASP.NET project, which crashes the formatter
    FILE            <file>                        a changed C# file to format
    OUTSIDE_SOLUTION <file>                       a FILE no project of SOLUTION owns; the formatter skips it
    FILE_LIST       <temporary file>              the FILE paths, one per line, for --include-file / --file-list
    SOLUTION        <solution> <score>            the solution to format and how many FILEs its projects own
    STOP            <reason>                      nothing to format; report the reason and stop

BASE is the pull request's base branch when gh reports one, else the remote's default branch (origin/HEAD), else
origin/main, else origin/master. SOLUTION is the nearest solution from the invocation directory that owns a FILE,
else the repository solution owning the most. A solution owning none of them is never chosen, because the
formatter would skip every file and report a clean result; that ends in STOP, as does finding no changed C# file
or no solution.

FILE, SKIPPED_ASPNET, and SOLUTION paths are relative to REPO_ROOT with forward slashes, because dotnet-format
runs from REPO_ROOT and matches --include paths against that directory.

Exits 0 with its lines, ending in SOLUTION or STOP. When the directory is not in a Git repository, a git command
fails, no base ref exists, or a file cannot be read or written, it prints `FAILED <reason>` as the last line and
exits 1. A usage error exits 2.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

WEB_PROJECT = re.compile(
    r"Microsoft\.NET\.Sdk\.Web|<WebApplication>|<WebSiteType>|349c5851-65df-11da-9384-00065b846f21",
    re.IGNORECASE,
)
SOLUTION_PROJECT = re.compile(r'^\s*Project\("[^"]*"\)\s*=\s*"[^"]*"\s*,\s*"([^"]+)"')
IGNORED_DIRECTORIES = frozenset({".git", "bin", "obj", "node_modules"})
REMOTE_REFS = "refs/remotes/origin/"
COMMAND_TIMEOUT_SECONDS = 300


@dataclass(frozen=True)
class Completed:
    returncode: int
    stdout: bytes


Runner = Callable[[Sequence[str], Path], Completed]


def subprocess_runner(arguments: Sequence[str], cwd: Path) -> Completed:
    environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        result = subprocess.run(
            list(arguments),
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return Completed(127, b"")
    return Completed(result.returncode, result.stdout)


@dataclass
class Services:
    """External effects, replaceable in tests."""

    run: Runner = subprocess_runner
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


def git_paths(services: Services, root: Path, *arguments: str) -> list[str]:
    result = services.run(["git", arguments[0], "-z", *arguments[1:]], root)
    if result.returncode != 0:
        raise Failed(f"git {' '.join(arguments)} failed")
    return [name for name in result.stdout.decode("utf-8", errors="surrogateescape").split("\0") if name]


def repository_root(services: Services, cwd: Path) -> Path:
    result = services.run(["git", "rev-parse", "--show-toplevel"], cwd)
    if result.returncode != 0:
        raise Failed(f"{cwd} is not inside a Git repository")
    return Path(result.stdout.decode("utf-8", errors="surrogateescape").strip()).resolve()


def ref_exists(services: Services, root: Path, ref: str) -> bool:
    return services.run(["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], root).returncode == 0


def pull_request_base(result: Completed) -> str | None:
    """The base branch in `gh pr view --json baseRefName` output, or None when gh does not report one."""
    if result.returncode != 0:
        return None
    try:
        document = json.loads(result.stdout.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    name = document.get("baseRefName") if isinstance(document, dict) else None
    if not isinstance(name, str) or not name.strip():
        return None
    return name.strip()


def remote_default_branch(result: Completed) -> str | None:
    """origin/<name> from `git symbolic-ref refs/remotes/origin/HEAD` output, or None when origin has no HEAD."""
    if result.returncode != 0:
        return None
    target = result.stdout.decode("utf-8", errors="surrogateescape").strip()
    name = target.removeprefix(REMOTE_REFS)
    return f"origin/{name}" if name and name != target else None


def base_ref(services: Services, root: Path) -> str:
    """The first that exists of the pull request's base branch when gh knows it, the remote's default branch
    (origin/HEAD, which clone sets), origin/main, and origin/master."""
    services.run(["git", "fetch", "origin", "--quiet"], root)
    candidates: list[str] = []
    if services.which("gh"):
        name = pull_request_base(services.run(["gh", "pr", "view", "--json", "baseRefName"], root))
        if name:
            candidates.append(f"origin/{name}")
    default = remote_default_branch(services.run(["git", "symbolic-ref", "--quiet", f"{REMOTE_REFS}HEAD"], root))
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


def solution_projects(solution: Path) -> set[str]:
    """Normalized absolute paths of the projects a .sln file lists."""
    projects: set[str] = set()
    for line in read_text(solution).splitlines():
        match = SOLUTION_PROJECT.match(line)
        if match:
            projects.add(same_path(solution.parent / match.group(1).replace("\\", "/")))
    return projects


def nearest_solutions(root: Path, cwd: Path) -> list[Path]:
    """Solutions in the nearest directory from cwd up to the root, or none."""
    inside = same_path(cwd) == same_path(root) or any(same_path(parent) == same_path(root) for parent in cwd.parents)
    directory = cwd if inside else root
    while True:
        found = sorted(path for path in directory.glob("*.sln") if path.is_file())
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
        found += [Path(current) / name for name in sorted(files) if name.casefold().endswith(".sln")]
    return found


def owns(solution: Path, owned: list[Path]) -> bool:
    projects = solution_projects(solution)
    return any(same_path(project) in projects for project in owned)


def best_solution(root: Path, candidates: list[Path], owners: list[list[Path]]) -> tuple[Path, int]:
    """The solution owning the most changed files; ties go to the shallowest, then by name."""
    ranked = sorted(
        ((sum(1 for owned in owners if owns(solution, owned)), solution) for solution in candidates),
        key=lambda item: (-item[0], len(item[1].relative_to(root).parts), relative(item[1], root).casefold()),
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
        raise Stop(f"no .sln found under {root}")
    best, score = best_solution(root, everything, owners)
    if not score:
        names = ", ".join(relative(solution, root) for solution in everything)
        raise Stop(f"changed files do not belong to any .sln found: {names}")
    return best, score


def resolve(cwd: Path, services: Services, emit: Callable[[str], None]) -> None:
    root = repository_root(services, cwd)
    emit(f"REPO_ROOT\t{root}")
    base = base_ref(services, root)
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
    for name, owned in zip(kept, owners):
        if not owns(solution, owned):
            emit(f"OUTSIDE_SOLUTION\t{name}")
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
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        resolve(arguments.cwd or Path.cwd(), services or Services(), print)
    except Stop as stop:
        print(f"STOP\t{stop}")
    except (Failed, OSError) as error:
        print(f"FAILED {error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
