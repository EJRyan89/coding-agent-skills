"""Resolve the changed C# files and the solution the dotnet-format skill formats.

    resolve [--cwd PATH]   print what to format; PATH is the directory the skill was invoked from, by default the
                           current directory

Output, one fact per line, tab separated:

    REPO_ROOT       <absolute repository root>
    FETCH_FAILED    <reason>                      `git fetch origin` failed; BASE is origin as last fetched
    BASE            <base ref the branch is compared with>
    FILE            <file>                        a changed C# file to format
    REFUSED         <path> <reason>               a changed file, or a solution the search of the repository
                                                  finds, that the formatter and the layout checker refuse by its
                                                  name, such as one with a segment Windows reserves as a device;
                                                  it is neither a FILE nor chosen
    OUTSIDE_SOLUTION <file>                       a FILE no project of SOLUTION owns; the formatter skips it
    SKIPPED_ASPNET  <file> <owning .csproj>       a FILE of an ASP.NET project, which the global tool is not run on
    FILE_LIST       <temporary file>              the FILE paths in UTF-8, each ended by a NUL, for --include-file
                                                  and --file-list
    SOLUTION        <solution> <score>            the solution to format and how many FILEs its projects own
    STOP            <reason>                      nothing to format; report the reason and stop

BASE is the pull request's base branch when gh reports one, else the remote's default branch (origin/HEAD), else
origin/main, else origin/master, after `git fetch origin` brings them up to date. SOLUTION is the nearest solution
(.sln or .slnx) from the invocation directory that owns a FILE, else the repository solution owning the most. A
solution owning none of them is never chosen, because the formatter would skip every file and report a clean result;
that ends in STOP, as does finding no changed C# file or no solution, or choosing a solution the .NET SDK's
`dotnet format` formats when dotnet is not installed.

The formatter is the .NET SDK's `dotnet format` for a .slnx solution, which the dotnet-format global tool cannot open,
and for a .sln whose C# and Visual Basic projects are all SDK-style files inside the repository, since the global tool
cannot load the analyzers current SDKs give such projects; it is the global tool for any other .sln, such as one holding
a .NET Framework project in the older format, on which the SDK's command can fail. A listed project outside the
repository by its text, or that a symbolic link or junction on its way, its own name included, leads out of it, judged
from each name's own metadata and each link's text, is never stat or opened, so it is not shown to be SDK-style and the
global tool formats its solution; `csharp_layout.py config` reports it as REFUSED. The global tool is not run on an
ASP.NET project's files, which SKIPPED_ASPNET names: the SDK's command formats them, so the skip holds only for the
global tool. A FILE that SKIPPED_ASPNET or OUTSIDE_SOLUTION names still goes into FILE_LIST, for the layout checker.
FILE_LIST ends each name with a NUL, the one character no path holds, so every name git lists reads back as written, a
leading space or a line separator inside it included. A printed line holds each value as ejryan89-skill-core's
`fact_line` flattens it, so a tab, a control character, or a line break in a name or a reason prints as a space and one
fact stays one line; the exact names are FILE_LIST's.

A changed file git lists, or a solution the search finds, is judged by its name as `listed_refusal` judges a FILE_LIST
entry or a --solution, before anything stats it, so resolve never lists a file or chooses a solution that
run_dotnet_format.py and csharp_layout.py then refuse: a name with a segment Windows reserves as a device, such as
`Con.cs` or a folder `Aux`, is REFUSED on every platform, though Linux and macOS hold it as an ordinary name, as the
runner and the layout checker refuse it there. A solution so named is passed over: the nearest solution is the nearest
one not refused, and one the search of the repository finds is reported as REFUSED. A changed name is judged before
anything looks for it, so a deleted file so named is REFUSED too. Only refused or deleted changed files, or only
refused solutions found, is a STOP.

FILE, SKIPPED_ASPNET, and SOLUTION paths are relative to REPO_ROOT with forward slashes, because dotnet-format
runs from REPO_ROOT and matches --include paths against that directory.

Its lines end in SOLUTION or STOP. When the directory is not in a Git repository, git is missing, a git command fails or
does not finish in time, no base ref exists, a changed file's name is not UTF-8, which FILE_LIST cannot hold, a file
cannot be read or written, or a changed file, a project beside one, or a solution it finds is a link leading outside the
repository, or lies through one, which it never stats or reads, it prints `FAILED <reason>` as the last line instead.

Exit status:
  0  the lines were printed, ending in SOLUTION or STOP
  1  a last line FAILED <reason>
  2  a usage error
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

import json_reader
import msbuild_xml
import private_folder
from console import run_main
from dotnet_solution import (
    SEARCH_SKIPPED_FOLDERS,
    is_solution,
    lexical_absolute,
    listed_projects,
    project_keys,
    project_path,
    refusal,
)
from executable_locator import find_program
from file_presence import ABSENT, NotAFile
from flat_text import fact_line, flat_text
from folder_walk import LINKED, LOOP, NESTED_CHECKOUT, followed_inside
from git_client import GitClient, GitError, GitResult, output_line
from github_client import GitHubClient, GitHubError, subprocess_runner
from github_client import Runner as GhRunner
from ordinal_case import has_suffix, ordinal_fold
from path_containment import resolved_path, within
from tree_reader import TreeReader
from xml_reader import XmlRefused, read_xml

# An ASP.NET project, whose files the dotnet-format global tool is not run on. The skill has skipped them since its
# first version as crashing the formatter, with no record of which project kind did; the SDK's dotnet format formats
# them, so `formatter_skips` holds the skip to the global tool alone.
WEB_PROJECT = re.compile(
    r"Microsoft\.NET\.Sdk\.Web|<WebApplication>|<WebSiteType>|349c5851-65df-11da-9384-00065b846f21",
    # ASCII: without it, ignoring case also matches the long s and the Kelvin sign as the s and k of `Sdk`, which
    # MSBuild, comparing ordinally ignoring case, keeps apart.
    re.IGNORECASE | re.ASCII,
)
# The project files the formatters format, whose style chooses between them; any other, such as a C++ project, is left
# out of the choice.
FORMATTED_PROJECT_SUFFIXES = (".csproj", ".vbproj")
SDK_FORMATTER = "dotnet format"
TOOL_FORMATTER = "dotnet-format"
REMOTE_REFS = "refs/remotes/origin/"
# Why a solution needs the .NET SDK's dotnet, after its name.
SDK_SOLUTION = "is a .slnx solution, or a .sln of SDK-style projects, which the .NET SDK's dotnet format formats"


# The reads made before the repository's tree is bound, or of a file that is the skill's own, which
# ejryan89-skill-core's tree_reader does not judge.
TREE_READ_ALLOWED = {
    "read_file_list": (
        "the FILE_LIST is the skill's own file, which resolve wrote under the private temporary directory and the "
        "caller names; it is no file of the repository"
    ),
    "given_folder": (
        "a folder the user names, the repository's top git gives, the folder the skill was invoked from, or a "
        "--repo-root, is resolved before any tree is bound, since it chooses the tree"
    ),
}


GIT_STATUS_ALLOWED = {
    "git_paths": "fails closed: refuses on any failure, ending the resolution",
    "fetch_failure": (
        "reports git's reason to the user: a failed fetch is a FETCH_FAILED line naming git's stderr, and the "
        "remote-tracking refs it left still serve"
    ),
}


def gh_runner_in(directory: Path) -> GhRunner:
    """A gh runner working in `directory`, since `gh pr view` reads the repository and branch from there."""
    return partial(subprocess_runner, cwd=directory)


@dataclass
class Services:
    """External effects, replaceable in tests."""

    git: GitClient = field(default_factory=GitClient)
    gh: Callable[[Path], GhRunner] = gh_runner_in
    # ejryan89-skill-core's locator, which finds a tool where `deploy.py check` does.
    which: Callable[[str], str | None] = field(default=find_program)


class Stop(Exception):
    """There is nothing to format; the message says why."""


class Failed(Exception):
    """The targets could not be resolved; the message says why."""


def write_file_list(names: list[str]) -> str:
    """Write `names` to a new temporary FILE_LIST, each in UTF-8 and ended by a NUL, and return its path.

    Raises Failed, before anything is written, for no names, which resolve stops on before it writes a list, and for
    a name that is not UTF-8, which git lists only on a file system that holds such names; every other name
    `read_file_list` returns as written."""
    if not names:
        raise Failed("there is no changed file to list")
    encoded: list[bytes] = []
    for name in names:
        try:
            encoded.append(name.encode("utf-8") + b"\0")
        except UnicodeEncodeError:
            raise Failed(
                f"the changed file {name!r} has a name that is not UTF-8, which FILE_LIST cannot hold"
            ) from None
    data = b"".join(encoded)
    descriptor, list_path = tempfile.mkstemp(
        prefix="dotnet-format-files-", suffix=".txt", dir=private_folder.temporary_directory()
    )
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
    return list_path


def read_file_list(path: Path) -> list[str]:
    """The paths a FILE_LIST names, as `write_file_list` wrote them: UTF-8, each ended by a NUL.

    Raises OSError when it cannot be read, and ValueError when it is not UTF-8, names no file, holds an empty name,
    or does not end in a NUL, as a list of lines does: resolve writes a FILE_LIST only when it found files to format,
    so an empty one did not come from it.
    """
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"{path} is not UTF-8 text") from None
    if not text:
        raise ValueError(f"{path} lists no files")
    if not text.endswith("\0"):
        raise ValueError(f"{path} does not end each name with a NUL, as resolve writes a FILE_LIST")
    files = text[:-1].split("\0")
    if "" in files:
        raise ValueError(f"{path} name {files.index('') + 1} is empty, which no path is")
    return files


LINKED_OUT = "its links cannot be followed inside the repository root"


def linked_out_refusal(root: Path, path: Path) -> str | None:
    """Why a script does not stat, read, or evaluate `path`, a path inside `root` by its text, or None: a symbolic link
    or junction on its way, its own name included, leads outside the root, a name on it cannot be examined or a link
    followed, or a `..` it holds climbs above the root once the links before it are followed, as Linux and macOS take
    it, such as after a link to the root itself. ejryan89-skill-core's `folder_walk.followed_inside` judges each name
    down from the root from its own metadata and each link from its text, so nothing outside the root is stat, listed,
    or opened to decide. A path whose links all stay inside, and one with a name that is not there, which its reader
    then reports, is None."""
    inside, linked = followed_inside(path, root)
    if linked is not None:
        return f"{LINKED_OUT}: {linked}"
    if inside is None:
        # `path` lies inside the root by its text, so followed_inside answers no place only for a `..` that climbs
        # above the root's top, a link before it having led there or not.
        return f"{LINKED_OUT}: a `..` in it climbs above the root once the links before it are followed"
    return None


def named_refusal(root: Path, name: str) -> str | None:
    """Why a script does not read the file a FILE_LIST entry or a --solution names, judged by its text alone, through
    ejryan89-skill-core's `refusal`, or None: one whose `..` segments leave `root`, a network-share or device form, or
    a segment Windows reserves as a device, on every platform. resolve judges each name it lists or chooses by it, so
    it never gives one that `listed_refusal` then refuses by its text."""
    base = lexical_absolute(root)
    return refusal(project_path(base, name), base)


def listed_refusal(root: Path, name: str) -> str | None:
    """Why a script does not read or write the file a FILE_LIST entry names, or None when it lies inside `root`.

    The entry is judged first as written, before anything touches it, so an absolute entry, even one naming a file
    inside the root, is refused unopened, and so, through `named_refusal`, are one whose `..` segments leave the root, a
    network-share or device form, and one with a segment Windows reserves as a device; then by its links
    (`linked_out_refusal`), so an entry inside the root that a symbolic link or junction leads out of it is refused too,
    with nothing outside the root touched to decide. An entry holding a `..` is judged by its links both as its text
    reads it, as Windows opens it, and as written, as Linux and macOS open it, taking each `..` after following the link
    before it, on every platform, so an entry either reading leads out of the root is refused wherever it runs."""
    base = lexical_absolute(root)
    path = project_path(base, name)
    refused = named_refusal(base, name)
    if refused:
        return refused
    if Path(name.replace("\\", "/")).anchor:
        return "the entry is an absolute path, and an entry names a file relative to the repository root"
    for named in dict.fromkeys((path, base / name.replace("\\", "/"))):
        linked = linked_out_refusal(base, named)
        if linked:
            return linked
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
            emit(fact_line("REFUSED", name, refused))
        else:
            kept.append(name)
    return kept


def link_refusal(root: Path, path: Path) -> str | None:
    """Why `path`, a file under `root` the script would write, is not written, or None: it, or a folder between it
    and `root`, is a link, so the write would land wherever the link points. A path that does not exist yet, such
    as an .editorconfig the script adds, is judged by the folders that do."""
    reader = TreeReader(root)
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        try:
            if reader.is_link(current):
                where = "the file" if current == path else f"its folder {current.relative_to(root).as_posix()}"
                return f"{where} is a symbolic link or junction, which the script does not write through"
        except FileNotFoundError:
            return None
        except OSError as error:
            return f"{current.relative_to(root).as_posix()} cannot be examined: {error.strerror or error}"
    return None


def write_refusal(root: Path, path: Path) -> str | None:
    """Why `path`, a file under `root` that a fix would rewrite, is not written, or None: `link_refusal`'s reason, or
    the file is read-only, which `atomic_file` refuses to replace."""
    refused = link_refusal(root, path)
    if refused is None and TreeReader(root).read_only(path):
        refused = "the file is read-only; make it writable to fix it"
    return refused


def relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def git(services: Services, directory: Path, *arguments: str) -> GitResult:
    """Run git in directory; git that is missing, cannot start, or does not finish fails the resolution."""
    try:
        return services.git.run(arguments, directory=directory)
    except GitError as exc:
        raise Failed(str(exc)) from exc


def git_paths(services: Services, root: Path, *arguments: str) -> list[str]:
    result = git(services, root, *arguments)
    if result.returncode != 0:
        reason = flat_text(result.stderr) or f"exit code {result.returncode}"
        raise Failed(f"git {' '.join(arguments)} failed: {reason}")
    return [name for name in result.stdout.split("\0") if name]


def repository_root(services: Services, cwd: Path) -> Path:
    try:
        top = services.git.rev_parse_path("--show-toplevel", directory=cwd)
    except GitError as exc:
        raise Failed(flat_text(str(exc))) from exc
    if top is None:
        raise Failed(f"{cwd} is not inside a Git repository")
    return given_folder(top)


def given_folder(path: Path) -> Path:
    """A folder the user names, or git names for them, made absolute with every link followed: the repository's top,
    the folder the skill was invoked from, or a --repo-root. It chooses the tree every later read stays inside, so it
    is resolved before any tree is bound."""
    return resolved_path(path)


def ref_exists(services: Services, root: Path, ref: str) -> bool:
    """Whether `ref` names a commit. Any git failure but its answer that `ref` names none, such as a broken ref or a
    configuration git cannot parse, fails the resolution with git's reason."""
    try:
        return services.git.commit_of(ref, directory=root) is not None
    except GitError as exc:
        raise Failed(f"git could not resolve {ref}: {flat_text(str(exc))}") from exc


def pull_request_base(services: Services, root: Path) -> str | None:
    """The base branch `gh pr view --json baseRefName` reports, or None when gh does not report one.

    Any gh failure gives None, and a rate limit is not waited out: the base is a hint with fallbacks after it.
    """
    try:
        output = GitHubClient(services.gh(root)).run(["pr", "view", "--json", "baseRefName"], retry=False)
        document = json_reader.loads(output.stdout)
    except (GitHubError, ValueError):
        return None
    name = document.get("baseRefName") if isinstance(document, dict) else None
    if not isinstance(name, str) or not name.strip():
        return None
    return name.strip()


def remote_default_branch(services: Services, root: Path) -> str | None:
    """origin/<name> from `git symbolic-ref refs/remotes/origin/HEAD`, or None when git answers that origin has no
    HEAD. Any other git failure fails the resolution with git's reason."""
    try:
        output = services.git.quiet_output(["symbolic-ref", "--quiet", f"{REMOTE_REFS}HEAD"], directory=root)
    except GitError as exc:
        raise Failed(f"git could not read {REMOTE_REFS}HEAD: {flat_text(str(exc))}") from exc
    if output is None:
        return None
    target = output_line(output)
    name = target.removeprefix(REMOTE_REFS)
    return f"origin/{name}" if name and name != target else None


def fetch_failure(services: Services, root: Path) -> str | None:
    """Why `git fetch origin` failed, or None when it succeeded.

    A failed fetch, offline or a remote that does not answer, leaves the remote-tracking refs as they were, which
    still serve as a base, so the caller reports it and goes on. A git that is missing, could not start, or could not
    end what it started never fetched at all and says nothing of origin, so it fails the resolution with its reason
    rather than read as a fetch that failed.
    """
    try:
        result = services.git.run(["fetch", "origin", "--quiet"], directory=root)
    except GitError as exc:
        # run raises no failure git answered, which a FETCH_FAILED line reports with git's stderr below, so no line of
        # git's is read here and the git directory is not asked for.
        if exc.origin_failed(None):
            return flat_text(str(exc))
        raise Failed(f"git could not fetch origin: {flat_text(str(exc))}") from exc
    if result.returncode == 0:
        return None
    return flat_text(result.stderr) or f"git fetch origin exited with code {result.returncode}"


def base_ref(services: Services, root: Path, emit: Callable[[str], None]) -> str:
    """The first that exists of the pull request's base branch when gh knows it, the remote's default branch
    (origin/HEAD, which clone sets), origin/main, and origin/master, after fetching origin."""
    failure = fetch_failure(services, root)
    if failure:
        emit(fact_line("FETCH_FAILED", failure))
    candidates: list[str] = []
    if services.which("gh"):
        name = pull_request_base(services, root)
        if name:
            candidates.append(f"origin/{name}")
    default = remote_default_branch(services, root)
    if default:
        candidates.append(default)
    candidates += ["origin/main", "origin/master"]
    for candidate in candidates:
        if ref_exists(services, root, candidate):
            return candidate
    raise Failed("no base ref: none of the pull request base, origin/HEAD, origin/main, or origin/master exists")


def changed_files(services: Services, root: Path, base: str, emit: Callable[[str], None]) -> list[str]:
    """Committed, uncommitted, and untracked C# files changed on this branch that still exist. One `named_refusal`
    refuses prints `REFUSED <file> <reason>` and is left out unstat'd, so a name Windows opens as a device is never
    opened; its name is judged before anything looks for it, so a deleted file so named is refused too, and a STOP ends
    the run when every changed file is so named or deleted. Raises Failed for one it cannot examine, a link leading
    outside the repository included, or that is something other than a file, which it cannot tell from a deleted one."""
    names = set(
        git_paths(services, root, "diff", "-z", "--name-only", "--diff-filter=AMR", f"{base}...HEAD", "--", "*.cs")
    )
    names |= set(git_paths(services, root, "diff", "-z", "--name-only", "HEAD", "--", "*.cs"))
    names |= set(git_paths(services, root, "ls-files", "-z", "--others", "--exclude-standard", "--", "*.cs"))
    kept: list[str] = []
    refused_any = False
    for name in sorted(names):
        refused = named_refusal(root, name)
        if refused:
            emit(fact_line("REFUSED", name, refused))
            refused_any = True
        elif still_there(root, name):
            kept.append(name)
    if not kept and refused_any:
        raise Stop(f"no C# file changed vs {base} is left to format: each is REFUSED by its name or deleted")
    return kept


def still_there(root: Path, name: str) -> bool:
    """Whether a changed file is still a file: False only when nothing is at its path, as for one deleted. Its name
    is judged by its links first (`linked_out_refusal`), so one that is, or lies through, a link leading outside the
    repository fails the resolution unstat'd, as one it cannot examine."""
    refused = linked_out_refusal(root, root / name)
    if refused:
        raise Failed(f"the changed file {name} cannot be examined: {refused}")
    try:
        return TreeReader(root).present(root / name)
    except OSError as error:
        raise Failed(f"the changed file {name} cannot be examined: {error}") from None


def listed_files(root: Path, folder: Path, wanted: Callable[[str], bool]) -> list[Path]:
    """The files directly in `folder`, a folder inside `root`, whose names `wanted` accepts, sorted: a folder or
    anything else by such a name is none, and so is every file of a folder that is not there. Raises Failed for a
    folder it cannot list, or such an entry it cannot examine, which could be one; it lists with iterdir, which raises
    there, where glob matches nothing. Such an entry that is a link leading outside `root` (`linked_out_refusal`) is
    one it cannot examine, and is never stat, so a project or solution behind it is never read. Its callers take
    `folder` from `TreeReader.climb`, which judged the path the climb started from, and every folder above that path
    inside `root` passes the same judgement."""
    reader = TreeReader(root)
    try:
        entries = reader.listing(folder)
    except ABSENT:
        return []
    except OSError as error:
        raise Failed(f"{folder} cannot be listed: {error}") from None
    found = []
    for path in entries:
        if not wanted(path.name):
            continue
        refused = linked_out_refusal(root, path)
        if refused:
            raise Failed(f"{path} cannot be examined: {refused}")
        try:
            if reader.present(path):
                found.append(path)
        except NotAFile:
            continue
        except OSError as error:
            raise Failed(f"{path} cannot be examined: {error}") from None
    return found


def is_project_name(name: str) -> bool:
    """Whether a name is a C# project's, as `glob("*.csproj")` matches one."""
    return fnmatch.fnmatch(name, "*.csproj")


def owning_projects(root: Path, file: Path) -> list[Path]:
    """The .csproj files in the nearest directory at or above the file, up to and including the root; a folder or
    project file it cannot examine, a project file that is a link leading outside the root included, fails, since
    passing it by could miss the ASP.NET project that owns the file, and reading it would leave the repository."""
    for folder in TreeReader(root).climb(file.parent):
        projects = listed_files(root, folder, is_project_name)
        if projects:
            return projects
    return []


def is_web_project(reader: TreeReader, project: Path) -> bool:
    """Whether a project file is an ASP.NET project's, read through `reader`."""
    return bool(WEB_PROJECT.search(reader.text(project)))


def is_sdk_style(reader: TreeReader, project: Path) -> bool:
    """Whether a project file is SDK-style, as ejryan89-skill-core's `msbuild_xml` reads one: its Project element names
    an Sdk, or it holds an Sdk element or an Import of an Sdk. A file that cannot be read is not shown to be one, and
    neither is one `reader` refuses."""
    try:
        return msbuild_xml.is_sdk_style(read_xml(reader.text(project)))
    except (OSError, XmlRefused):
        return False


def formatter_for(root: Path, solution: Path) -> str:
    """The formatter for a solution: the SDK's `dotnet format` for a .slnx, and for a .sln listing C# or Visual Basic
    projects that are all SDK-style files inside `root`; the dotnet-format global tool for any other .sln, one it
    cannot read included, so that only a solution shown to suit the SDK's command gets it. A listed project outside
    `root` by its text (`refusal`), or that a link on its way leads out of it (`linked_out_refusal`), is never stat or
    opened, so it is not shown to be SDK-style and the solution gets the global tool; `csharp_layout.py config` reports
    it as REFUSED. A relative root or solution is taken from the working directory, as the formatter takes them."""
    if has_suffix(solution.name, ".slnx"):
        return SDK_FORMATTER
    root, solution = lexical_absolute(root), lexical_absolute(solution)
    reader = TreeReader(root)
    try:
        listed = listed_projects(reader.text(solution), solution)
    except (OSError, XmlRefused):
        return TOOL_FORMATTER
    projects = [
        project
        for entry in listed
        if has_suffix((project := project_path(solution.parent, entry)).name, FORMATTED_PROJECT_SUFFIXES)
    ]
    if projects and all(
        refusal(project, root) is None and linked_out_refusal(root, project) is None and is_sdk_style(reader, project)
        for project in projects
    ):
        return SDK_FORMATTER
    return TOOL_FORMATTER


def web_project(root: Path, name: str) -> Path | None:
    """The ASP.NET project owning a file, of those `owning_projects` gives, or None."""
    reader = TreeReader(root)
    return next((project for project in owning_projects(root, root / name) if is_web_project(reader, project)), None)


def formatter_skips(root: Path, formatter: str, names: list[str]) -> dict[str, Path]:
    """The files `formatter` is not run on, each with the ASP.NET project owning it: none for the SDK's command,
    which formats them, and every ASP.NET project's file for the global tool."""
    if formatter != TOOL_FORMATTER:
        return {}
    return {name: project for name in names if (project := web_project(root, name)) is not None}


def solution_name_refusal(root: Path, solution: Path) -> str | None:
    """`named_refusal` of the --solution resolve would print for `solution`, a solution inside `root`."""
    return named_refusal(root, relative(solution, root))


def nearest_solutions(root: Path, cwd: Path) -> list[Path]:
    """Solutions in the nearest directory from cwd up to the root that holds one `solution_name_refusal` does not
    refuse, those alone, or none; one that is a link leading outside the root fails, as `listed_files` says."""
    for folder in TreeReader(root).climb(cwd if within(cwd, root) else root):
        found = listed_files(root, folder, lambda name: is_solution(Path(name)))
        kept = [solution for solution in found if solution_name_refusal(root, solution) is None]
        if kept:
            return kept
    return []


def find_solutions(root: Path) -> list[Path]:
    """Every solution in the repository, in ejryan89-skill-core's folder walk's order, outside the folders the .NET
    search skips (`dotnet_solution.SEARCH_SKIPPED_FOLDERS`) and nested checkouts, never through a junction or symbolic
    link.

    A folder it cannot list, or an entry it cannot examine, is an error, not an empty one: skipping it could hide the
    solution to choose. A linked folder is not one, since the link may lead out of the repository or back into it. The
    walk is given the root as its tree, so it follows no link entry out of the repository, not even to learn what it
    is, and a solution that is a link leading out (`linked_out_refusal`) is one it cannot examine, never read.
    """
    walk = TreeReader(root).search(root, lambda name: is_solution(Path(name)), skipped=SEARCH_SKIPPED_FOLDERS)
    for entry in walk.not_entered:
        if entry.kind not in (LINKED, LOOP, NESTED_CHECKOUT):
            raise Failed(f"{entry.path}: {entry.reason}")
    for solution in walk.files:
        refused = linked_out_refusal(root, solution)
        if refused:
            raise Failed(f"{solution} cannot be examined: {refused}")
    return walk.files


def owns(root: Path, solution: Path, owned: list[Path]) -> bool:
    """Whether the solution lists a project that owns the file; a solution that cannot be read fails, since taking
    it for one that lists no project would name the wrong cause. Each entry is keyed by ejryan89-skill-core's
    `dotnet_solution.project_keys`, so an entry outside `root`, or that a link leads out of it, is compared by its
    text, and no folder outside the root is listed to compare it."""
    reader = TreeReader(root)
    try:
        projects = project_keys(reader.text(solution), solution, root)
    except XmlRefused as error:
        raise Failed(f"{solution} cannot be read: {error}") from None
    return any(reader.key(project) in projects for project in owned)


def best_solution(root: Path, candidates: list[Path], owners: list[list[Path]]) -> tuple[Path, int]:
    """The solution owning the most changed files; ties go to the shallowest, then to a .sln over a .slnx (the
    dotnet-format global tool opens only a .sln), then by name."""
    ranked = sorted(
        ((sum(1 for owned in owners if owns(root, solution, owned)), solution) for solution in candidates),
        key=lambda item: (
            -item[0],
            len(item[1].relative_to(root).parts),
            has_suffix(item[1].name, ".slnx"),
            ordinal_fold(relative(item[1], root)),
        ),
    )
    best_score, best = ranked[0]
    return best, best_score


def choose_solution(root: Path, cwd: Path, owners: list[list[Path]], emit: Callable[[str], None]) -> tuple[Path, int]:
    """The nearest solution that owns a changed file, else the repository solution owning the most.

    A solution that owns none of the changed files is never chosen: the formatter would skip every file and
    report a clean result. Nor is one `solution_name_refusal` refuses, which the runner would refuse; each the search
    of the repository finds prints `REFUSED <solution> <reason>`.
    """
    nearest = nearest_solutions(root, cwd)
    if nearest:
        best, score = best_solution(root, nearest, owners)
        if score:
            return best, score
    found = find_solutions(root)
    everything: list[Path] = []
    for solution in found:
        refused = solution_name_refusal(root, solution)
        if refused:
            emit(fact_line("REFUSED", relative(solution, root), refused))
        else:
            everything.append(solution)
    if found and not everything:
        raise Stop(f"every .sln or .slnx found under {root} is REFUSED")
    if not everything:
        raise Stop(f"no .sln or .slnx found under {root}")
    best, score = best_solution(root, everything, owners)
    if not score:
        names = ", ".join(relative(solution, root) for solution in everything)
        raise Stop(f"changed files do not belong to any solution found: {names}")
    return best, score


def resolve(cwd: Path, services: Services, emit: Callable[[str], None]) -> None:
    root = repository_root(services, cwd)
    emit(fact_line("REPO_ROOT", root))
    base = base_ref(services, root, emit)
    emit(fact_line("BASE", base))
    kept = changed_files(services, root, base, emit)
    if not kept:
        raise Stop(f"no C# files changed vs {base}")
    owners = [owning_projects(root, root / name) for name in kept]
    for name in kept:
        emit(fact_line("FILE", name))
    solution, score = choose_solution(root, given_folder(cwd), owners, emit)
    formatter = formatter_for(root, solution)
    skipped = formatter_skips(root, formatter, kept)
    for name, owned in zip(kept, owners, strict=True):
        if not owns(root, solution, owned):
            emit(fact_line("OUTSIDE_SOLUTION", name))
        elif name in skipped:
            emit(fact_line("SKIPPED_ASPNET", name, relative(skipped[name], root)))
    if formatter == SDK_FORMATTER and not services.which("dotnet"):
        raise Stop(f"{relative(solution, root)} {SDK_SOLUTION}; install the .NET SDK 9.0.200 or newer")
    emit(fact_line("FILE_LIST", write_file_list(kept)))
    emit(fact_line("SOLUTION", relative(solution, root), score))


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
        print(fact_line("STOP", stop))
    except (Failed, OSError) as error:
        print(f"FAILED {flat_text(str(error))}")
        return 1
    return 0


if __name__ == "__main__":
    run_main(main, errors="replace")
