"""A read-only dependency graph of a .NET solution: why a package or assembly is there, where an assembly is used,
which versions and binding redirects disagree, and the whole graph as a diagram.

    why <name> [<name> ...] [--root PATH] [--project PATH]   every path from each project to each name
    why --all --project PATH [--root PATH]                  everything one project reaches, with the first hop
    where <assembly> [--root PATH] [--project PATH]         the projects whose references resolve to the assembly
    conflicts [--root PATH] [--project PATH]                versions wanted at odds, and binding redirects to check
    graph [--format dot|mermaid] [--output FILE] [--root PATH] [--project PATH]   the graph, written to a file

--root is a .sln or .slnx, a directory holding one, or a directory to search for *.csproj files; by default the
current directory. --project narrows a query to one project of the solution, and `graph` to that project and the
projects it reaches. Output is one tab-separated fact per line, paths relative to the solution's directory with
forward slashes:

    ROOT          <solution or directory>
    PROJECT       <project> <sdk|legacy> <assets|nuspec|direct> <listed|referenced>
                  where package-to-package edges came from: obj/project.assets.json, the packages' nuspecs in the
                  packages folder or the NuGet cache, or neither, leaving the direct references alone
    UNREADABLE    <file> <reason>                            a file the readers refused, a path a file names
                  outside the examined tree, a file a link on its way leads outside it, naming the link and its
                  target, more than 40 links on its way, or a link whose target cannot be read, a link the search
                  lists that leads outside it, a condition, name, metadata value, or AssemblyName they cannot
                  settle, an Import not read, an item inside a Target, or a folder the search for projects does
                  not enter, a junction, a symbolic link, a nested checkout, one it cannot list, or the one its
                  bound stopped it at; its facts are missing
    SDK_FAILED    <project> <name> <reason>                  `dotnet nuget why` failed, or was not asked since the
                  .NET host or MSBuild would read a file the reader refused or cannot place; the graph walk answered
    PATH          <project> <frameworks> <name> <sdk|graph> <hop> > <hop> ...    one chain, each hop
                  `name (vversion)`, `name (project)`, `name (vversion, assembly)`, `name (assembly)` for an
                  assembly a package ships, or `name (vversion, graph vother)` for a package the SDK resolves at one
                  version and the walk at another, every other version the walk took joined by `;` as in
                  `graph v12.0.3;v11.0.0`; the source says whether `dotnet nuget why` answered or the walk did
    TRUNCATED     <project> <frameworks> <name> <limit>      more chains than the limit, the SDK's counted; the
                  first ones printed
    MISSING       <name>                                     no project reaches the name
    REACHES       <project> <frameworks> <name> <version> <package|project|assembly|framework> <first hop>
    WHERE         <project> <frameworks> <assembly> <reference|framework|package|project> <version> <via>
    CONFLICT      <name> <package|assembly> <version> <project> <frameworks> <via>   one line per side
    REDIRECT_STALE <project> <config> <assembly> <newVersion> <versions referenced>
    REDIRECT_UNREFERENCED <project> <config> <assembly> <newVersion>
    WROTE         <file> <nodes> <edges>
    SUMMARY       the result: why prints <paths> <names found> <names missing>, or with --all <items reached>;
                  where <projects>; conflicts <names in conflict> <stale redirects>; graph <nodes> <edges>

<frameworks> names the target frameworks of the project's restore that hold the line, joined by `;`, or `-` for a
project with no restore, whose facts are not kept per framework. Each project is evaluated as MSBuild evaluates it,
once per build (msbuild_evaluation's docstring): its files and the files it imports in import order for the
properties, then its items with the final properties. A project with no restore counts every Include and Update that
may apply in one of its builds, and a Remove only where it applies in every build, and one that cannot be settled in
a build and fails in none is reported. A line that holds for several frameworks is printed once. Two frameworks of one
project resolving different versions build apart and are not a conflict. A framework is named as the project names
it, whichever form its restore writes (`net8.0` or `.NETCoreApp,Version=v8.0`). A framework's direct packages are those
its restore recorded, and its project references are the project file's ProjectReference items its restore also
recorded; for a framework the restore did not record them for, and for its assembly references, which a restore never
records, they are the items that framework's build leaves, a package without a version of its own taking the central
PackageVersion that build leaves. An item, or a metadata value, whose condition or name the reader cannot settle is
not applied and is reported as an UNREADABLE line naming it and what was done instead; an item inside a Target, which
MSBuild defines only when the target runs, is left out and reported so, and so is an Import the reader does not read.

A directory holding no solution is searched for *.csproj files below it, in ejryan89-skill-core's folder walk,
outside the folders the .NET search skips (dotnet_solution's SEARCH_SKIPPED_FOLDERS) and outside hidden folders, whose
name starts with a dot and which hold a tool's state rather than a project: the search prunes them, listing nothing
inside them. The search is bounded rather than refused outside a checkout, since a legacy solution may sit in no git
checkout at all and a checkout may itself be large: it lists at most 10000 folders (PROJECT_SEARCH_FOLDER_LIMIT), the
directory included, so a search started in a large folder, such as a home folder, ends. Past the bound it stops and
reports the next folder it would have listed as an UNREADABLE line, which, with every folder after it in the walk's
order, was not searched; with no project found before it, the FAILED line names it. A solution is read as it lists its
projects, with no search. A --project naming a .csproj below a searched directory that the search did not reach is
not read, since --project narrows a query to a project of the root and reading it would read what the search keeps
out; the FAILED line says why the search did not reach it instead:

    FAILED <project> lies in <folder>, which the search for projects under <directory> does not enter: <reason>; ...
    FAILED <project> lies past the search for projects under <directory>, which stopped at <folder>: <reason>; ...

the first naming the first folder on its way down that the search skips, prunes as hidden, or does not enter, the
second the folder the bound stopped the search at, each ending with the advice to pass --root at the project's
solution or its folder, and, when the search found no project at all, with each folder it did not search, as the
FAILED line of a search finding none names them. A project named through a link to its file is the project the search
found there. A project whose own stat fails is refused as one that cannot be examined, before the search is asked
why it did not reach it; a project below a link on its way, or one that is itself a link, is never stat, since the
link may lead outside the examined tree, so the folder the search passed over is named whether or not the file is
there; so is one below a folder whose metadata cannot be read. The --project argument's part up to its last `..` is
followed inside the tree as the system follows it, never through a link out of it, and the whole path is resolved, to
match a project found through a link inside the tree, only when it lies in the tree and no link on its way leads out of
it; a path a link or `..` leads out of the tree, or one named outside it, is no project of the root and is never
resolved in full. A limit: a --project named outside the tree, through a link above it such as macOS's /var, or with its
own `..` leaving the tree before a link, has the folders leading to it resolved to learn whether one of them is the
root, and its part up to its last `..` resolved, which reads the metadata of each name on its way, through such a link
too, and lists each folder on the way, reading the metadata of a case-swapped name in it, to learn whether it compares
names by case, but reads no file's contents; `repo/../repo/Linked/Sub/../Other/Other.csproj`, with Linked leading
outside, is resolved through Linked and then refused.

`why` asks `dotnet nuget why` for a project that is SDK-style, uses PackageReference, has an obj/project.assets.json no
older than the project file, and only when dotnet is installed. A run that fails, even after printing part of an answer,
is an SDK_FAILED line. The SDK runs in the project's folder and evaluates the project through MSBuild, so a project is
never passed to it when they would read a file the reader refused for where it lies, outside the examined tree or
through a link leading out of it: an import, one the reader does not read because its condition cannot be settled or its
path is a wildcard included, a nearest Directory.Build.props, Directory.Packages.props, or Directory.Build.targets, a
*OfFileAbove start or candidate, an Exists() path, the restore's <project file>.nuget.g.props, .nuget.g.targets, or
assets file where the files leave $(MSBuildProjectExtensionsPath), $(BaseIntermediateOutputPath), and
$(ProjectAssetsFile) (msbuild_evaluation's docstring), the project's .user file, or the nearest global.json at or above
the project's folder, which the .NET host reads to pick the SDK and MSBuild's resolvers read too; nor when they may read
a file whose path the reader cannot settle, an Import's, such as one naming a property no file sets, or one of the
restore's, such as after an Import the reader did not read, which may set its folder; nor when a global.json candidate
cannot be stat. The walk answers that project alone, and an SDK_FAILED line for each name says why, naming the first
such file. The graph walk runs for every project too: for a project the SDK answered, the walk's chains along a route
the SDK printed are printed once with it, named `sdk`, with the walk's hops at the SDK's versions, and the walk's
version of a package after `graph` where a referenced project's own restore resolves it apart, a split `conflicts`
reports; the walk adds every chain the SDK cannot see, such as one ending in an assembly or one through a
packages.config project; for every other project it answers alone. So whether dotnet is installed changes which source a
route names and which version leads where the two differ, never which routes are printed. A path a solution or project
names is read only inside the examined tree, the checkout holding the solution or directory searched, and every read
only of a regular file within ejryan89-skill-core's READ_LIMIT_BYTES (msbuild_reader's docstring). Nothing inside the
tree is stat, listed, or read through a link that leads outside it, judged from each name's own metadata and each link's
text (ejryan89-skill-core's folder_walk.outside_link_refusal). The search for projects follows no link entry out of the
tree, not even to learn what it leads to: a .csproj so linked is an UNREADABLE line naming the link and its target, and
any other such entry is reported as a link the search does not follow, so a file link the script also reads, such as a
linked Directory.Packages.props, is named twice, by the search and by the read. A --root is judged as named, before it
is stat or resolved: the checkout holding it, the innermost one, nearest at or above it, is found from the top down
(msbuild_reader.examined_tree), following a link on its way only while no checkout lies above it, and taken as it stands
before the first link inside a checkout, so a link above the checkout, such as macOS's /var or a link to the place a
repository is kept, is the user's own choice of root and is followed, while a link inside the checkout is never followed
to look for a marker below it. A solution or folder --root names that such a link leads out of the checkout, and a
solution in the root folder so linked, is a FAILED line naming the link; one whose link stays inside the checkout is
read through. A `..` in --root is taken as the system takes it, inside the checkout (followed_inside): one after a link
steps back from where the link leads, so a link before it that leads out of the checkout fails the run the same way, and
one that climbs out of the checkout at its top leaves it there and is judged again in the folder it reaches. A project
the search finds or a solution lists, an import, the nearest Directory.Build.props or Directory.Packages.props, a
restore's assets, a packages.config, a config, the packages folder, a package's folder, its lib folder, or its nuspec,
that a link on its way, its own name included, leads outside the tree is an UNREADABLE line naming the link and its
target; and an Exists() condition below such a link is reported and unsettled. A link whose target stays inside the tree
is read through. The NuGet cache lies outside the tree and is read where it is, resolved first, through a reader bound
to the cache folder, so a link inside it that leads out of it is reported, not followed. A path a file names outside the
tree is compared with another by its text, never by asking its folders how they compare names, so no folder outside the
tree is listed to compare it. Limits: the names of the assemblies below a package's lib folder are found by the reader's
search, which enters no folder link, so the assemblies below one that stays inside are not named, a Windows junction
among them, and no folder holding a `.git` entry, which it takes for a nested checkout, reading no file's contents; a
wildcard Import is judged as text and by its folder
before the wildcard, since the reader expands none, so a link a wildcard matches is not judged, nor a link named after a
`..` that follows a wildcard, as in `../*/../Shared/Common.props`, and so, of the restore's files, only the NuGet ones
are judged, though MSBuild imports every <project file>.*.props and .targets in that folder; the restore's folder no
file sets is taken as obj/, though the environment or the command line may set it; the search for global.json stops, as
MSBuild's searches do, at anything of that name but a folder, a link to nothing included, which the .NET host may pass
over to read one further up; an Exists() or *OfFileAbove path the reader cannot settle is stat by MSBuild wherever it
leads, though no file there is read; the restore's generated files, the project's .user file, and the nearest
global.json are judged by where they lie, never by what they hold, so an Import or a ProjectAssetsFile a generated file
holds, a global.json's sdk.paths, which the .NET host honours from SDK 10, or its msbuild-sdks, that leads outside the
tree is not seen, nor what the NuGet SDK resolver reads for a versioned MSBuild SDK, every NuGet.Config from the
project's folder up and the user's and the machine's, or what it downloads; a link inside the checkout whose absolute
target names the checkout through another spelling of its path, such as /tmp for macOS's /private/tmp, is refused as
leading out of it, since a target is taken as inside only when it names the checkout as the checkout's own path is
written; a `..` after a link or after a name that is not there, in --root or --project, is judged as Linux and macOS
take it, after following the link, and refused as missing after a name not there, so on Windows a path the system would
take by name can be refused; and the look at the links and the read are two steps (folder_walk.followed_inside), so a
name replaced by a link between them is read through, and a hard link, being the file itself, is read as a file of the
tree wherever its other names lie. The script changes nothing on disk but the graph file and runs no restore or build;
it prints a last line FAILED <reason> when it cannot finish, such as when the root cannot be resolved, a solution found
in the root folder, or a solution or folder --root names, is one a link inside the checkout leads out of it (`<root>
cannot be examined: <reason>`), --project names no project of the root, --output is inside a skills directory, the home
folder is not known, or the graph file cannot be written.

Exit status:
  0  the query or graph ran and printed no CONFLICT, REDIRECT_STALE, or UNREADABLE line
  1  a CONFLICT, REDIRECT_STALE, or UNREADABLE line, or a last line FAILED <reason>
  2  a usage error
"""

from __future__ import annotations

import argparse
import shutil
import stat
import sys
import tempfile
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import file_presence
import private_folder
from atomic_file import replace_text
from console import run_main
from dotnet_solution import SEARCH_SKIPPED_FOLDERS, is_solution, lexical_absolute, project_path, refusal, same_path
from executable_locator import RelativeEnvironmentPath, absolute_home, environment_path
from flat_text import fact_line, flat_text
from folder_walk import BOUNDED, NotEntered, followed_inside, is_hidden, names_below, outside_link_refusal
from msbuild_evaluation import Build, ProjectFile, Sources, UnplacedImport, Unsettled, read_project, tree_key
from msbuild_reader import (
    ASSETS_FILE,
    CONFIG_FILES,
    PACKAGES_CONFIG,
    AssemblyRef,
    Assets,
    PackageNode,
    PackageRef,
    Refused,
    Unreadable,
    examined_tree,
    matching_framework,
    nearest_file,
    os_unreadable,
    package_assemblies,
    read_assets,
    read_nuspec,
    read_packages_config,
    read_redirects,
    read_solution,
)
from ordinal_case import ordinal_fold
from path_containment import resolved_path, within
from sdk_why import DEFAULT_TIMEOUT_SECONDS, SdkPath, Services, run_why
from skill_roots import skills_directory_holding
from tree_reader import TreeReader

PROJECT_SUFFIX = ".csproj"
# The most folders the search for projects lists, the searched directory included, as the module states.
PROJECT_SEARCH_FOLDER_LIMIT = 10_000
PATH_LIMIT = 200
FINDING_KEYS = ("CONFLICT", "REDIRECT_STALE", "UNREADABLE")
NO_FRAMEWORK = "-"
# What a collection compares a project's path by (`Graph.key`).
PathKey = Callable[[Path], str]
Hop = tuple[str, str, str]  # kind (package, project, assembly, framework), name, version or ""
Row = tuple[str, ...]


# The presence reads and removals here that read a path they cannot stat, list, or remove as nothing there,
# each with why that is the answer it should give.
PRESENCE_ALLOWED = {
    "write_graph": "removes the folder it made, part way filled, before raising the failure that stopped it, which an "
    "error of the removal must not replace",
}


class Failed(Exception):
    """The query could not run; the message says why."""


@dataclass
class Target:
    """One framework's references and package closure of a project: the restore's target framework, or `-` for a
    project with no restore, whose references and nuspec edges hold for every framework it builds."""

    framework: str
    nodes: dict[str, PackageNode] = field(default_factory=dict)  # case-folded id -> node
    direct: list[PackageRef] = field(default_factory=list)
    project_refs: list[Path] = field(default_factory=list)
    assemblies: list[AssemblyRef] = field(default_factory=list)

    def node(self, package: str) -> PackageNode | None:
        return self.nodes.get(ordinal_fold(package))


@dataclass
class Project:
    """A project of the solution with the package closure its own files give, one target per framework. A project
    whose file could not be read is kept, unreadable, so a reference to it still names it."""

    file: ProjectFile
    listed: bool
    readable: bool = True
    targets: list[Target] = field(default_factory=lambda: [Target(NO_FRAMEWORK)])
    edges: str = "direct"
    sdk_answers: bool = False
    # Why a project the SDK would answer is not asked (`sdk_withheld`), the reason its SDK_FAILED line gives.
    sdk_withheld: str | None = None

    def targets_for(self, framework: str) -> list[Target]:
        """The targets a walk under `framework` enters: the target of that framework, else every one."""
        match = matching_framework(framework, (target.framework for target in self.targets))
        return [target for target in self.targets if target.framework == match] or self.targets

    def references_for(self, framework: str, key: PathKey) -> tuple[list[Path], list[AssemblyRef]]:
        """The projects and assemblies the targets a walk under `framework` enters reference, each once, a project by
        `key`."""
        return references_of(self.targets_for(framework), key)


# The containment tests that are not decisions, each with why it does not call path_containment.
CONTAINMENT_ALLOWED = {
    "Graph.relative": "display only: a path outside the solution's directory is named absolute rather than refused",
}


@dataclass
class Graph:
    root: Path
    base: Path
    tree: Path  # the examined tree, outside which no path a file names is read (`refusal`)
    projects: dict[str, Project] = field(default_factory=dict)  # key -> project
    unreadable: list[tuple[Path, str]] = field(default_factory=list)
    passed_over: list[NotEntered] = field(default_factory=list)  # the folders the search for projects did not enter
    sources: Sources | None = None  # the files the projects are evaluated from, each parsed once
    reader: TreeReader = field(init=False)  # the examined tree's reader, through which every file is stat and read

    def __post_init__(self) -> None:
        self.reader = TreeReader(self.tree)

    def relative(self, path: Path) -> str:
        """`path` relative to the solution's directory, made absolute without reaching the file system, so naming a
        refused path never opens it."""
        absolute = lexical_absolute(path)
        try:
            return absolute.relative_to(self.base).as_posix()
        except ValueError:
            return absolute.as_posix()

    def label(self, project: Project) -> str:
        return self.relative(project.file.path)

    def key(self, path: Path) -> str:
        """What a collection compares a path a file names by (`tree_key`): its text when it lies outside the examined
        tree, so no folder outside it is listed to compare it."""
        return tree_key(path, self.tree)

    def project_at(self, path: Path) -> Project | None:
        return self.projects.get(self.key(path))

    def refuse(self, error: Unreadable) -> None:
        """Report a file the readers refused, each file and reason once, so a file several projects import, such as
        an unreadable Directory.Packages.props, is reported once."""
        key = (self.key(error.file), error.reason)
        if key not in {(self.key(file), reason) for file, reason in self.unreadable}:
            self.unreadable.append((error.file, error.reason))

    def refuse_unsettled(self, project: Project, unsettled: Iterable[Unsettled], kinds: Iterable[str]) -> None:
        """Report each item of `kinds`, or metadata value of one, a build did not apply because it could not settle
        it, saying what it did instead."""
        wanted = set(kinds)
        for entry in unsettled:
            if entry.kind in wanted:
                subject = self.relative(entry.path) if entry.path is not None else entry.name
                self.refuse(Unreadable(project.file.path, entry.reason(subject)))

    def build_of(self, project: Project, framework: str) -> Build:
        """The project's build of a framework, with what its evaluation could not read reported."""
        build = project.file.build(framework)
        for report in build.reports:
            self.refuse(report)
        return build


def references_of(targets: Iterable[Target], key: PathKey) -> tuple[list[Path], list[AssemblyRef]]:
    """The projects and assemblies the targets reference, each once, in the order first met, a project by `key`."""
    chosen = list(targets)
    projects = {key(path): path for target in chosen for path in target.project_refs}
    return list(projects.values()), list(dict.fromkeys(item for target in chosen for item in target.assemblies))


class Emitter:
    """Prints each fact line and remembers whether a finding was among them."""

    def __init__(self, emit: Callable[[str], None]) -> None:
        self.emit = emit
        self.findings = 0

    def line(self, key: str, *fields: object) -> None:
        if key in FINDING_KEYS:
            self.findings += 1
        self.emit(fact_line(key, *fields))


def find_projects(
    directory: Path, unreadable: list[tuple[Path, str]], passed_over: list[NotEntered] | None = None
) -> list[Path]:
    """Every .csproj under the directory, in ejryan89-skill-core's folder walk's order, each folder's projects before
    its subfolders', outside the folders the .NET search skips (`dotnet_solution.SEARCH_SKIPPED_FOLDERS`) and the hidden
    folders it prunes, within PROJECT_SEARCH_FOLDER_LIMIT folders. A folder the walk does not enter, a junction or a
    symbolic link, which may lead outside the directory or back into it, a nested checkout, or one that cannot be
    listed, is reported, and costs only the projects it holds; the folder the bound stopped it at is reported too, and
    costs the projects in it and in every folder after it in the walk's order. Each is added to `passed_over` too, so
    `unsearched` can say why a project below one was not found. No link entry is followed out of the checkout holding
    the directory (`checkout_tree`): a .csproj so linked is given to `load_projects`, which reports it, and any other
    such entry is reported as a link the search does not follow."""
    walk = TreeReader(checkout_tree(directory)).search(
        directory,
        lambda name: ordinal_fold(name).endswith(PROJECT_SUFFIX),
        skipped=SEARCH_SKIPPED_FOLDERS,
        prune=is_hidden,
        limit=PROJECT_SEARCH_FOLDER_LIMIT,
    )
    unreadable += [(entry.path, entry.reason) for entry in walk.not_entered]
    if passed_over is not None:
        passed_over += walk.not_entered
    return walk.files


SKIPPED_REASON = "the .NET search skips a folder so named, a version-control, dependency, build-output, or IDE folder"
HIDDEN_REASON = "the folder is hidden, its name starting with a dot, which the search prunes"
RETRY_ADVICE = "pass --root at its solution or its folder"
SDK_NOT_ASKED = (
    "dotnet nuget why was not asked, since MSBuild would reach {file}, which the reader refused: it lies outside the"
    " examined tree or a link on its way leads out of it"
)
SDK_UNPLACED = (
    "dotnet nuget why was not asked, since MSBuild may read the Import of {written} in {file}, whose path the reader"
    " cannot settle"
)
SDK_UNPLACED_GENERATED = (
    "dotnet nuget why was not asked, since MSBuild reads the restore's {written} for {file}, whose path the reader"
    " cannot settle"
)
SDK_NOT_JUDGED = (
    "dotnet nuget why was not asked, since the reader cannot tell whether the .NET host reads {file}: {reason}"
)
GLOBAL_JSON = "global.json"


def followed_named(path: Path, tree: Path) -> tuple[Path | None, str | None]:
    """Where a path the user names, a --root or a --project, leads inside `tree`, or why not: `followed_inside`, which
    takes each `..` after following the link before it, from the link's text, as Linux and macOS take it, on every
    platform, and stats nothing outside the tree. A name that is not there before a `..` leaves a path Linux and macOS
    cannot open, so it is refused as missing, on every platform, though Windows takes such a `..` by name
    (`file_presence.MISSING`), and nothing past the missing name is resolved, which would follow a link after it. A
    `..` that leaves the tree at its top is refused when a link brought the path there (`climb_through_link`), and
    answers None and None, as `followed_inside` does, only when the path's own text stands at the top."""
    inside, refused = followed_inside(path, tree)
    if inside is None and refused is None:
        return None, climb_through_link(path, tree)
    if inside is not None and ".." in inside.parts:
        return None, file_presence.MISSING
    return inside, refused


def climb_through_link(path: Path, tree: Path) -> str | None:
    """Why the `..` that takes `path` out of `tree` at its top is refused, or None when the path's own text stands at
    the top there, so the `..` climbs out by its text alone. `followed_inside` answers None and None for such a `..`
    whether the path's names reached the top by text or through a link whose text leads there, such as one to `.`, to
    `..` from a folder below the top, or to the tree's own absolute path; the system takes the `..` after following
    that link, so where the text stands elsewhere, the path leaves the tree through the link and is refused, never
    taken as a climb out of the tree's top to the folder above it."""
    root = tree.absolute()
    names = names_below(path.absolute(), root)
    for index, name in enumerate(names or ()):
        if name != "..":
            continue
        before = root.joinpath(*names[:index]) if names else root
        if followed_inside(before / "..", tree) != (None, None):
            continue
        if lexical_absolute(before) == lexical_absolute(root):
            return None
        return (
            f"{before.as_posix()} is reached through a link, so the .. after it leaves the examined tree "
            f"{root.as_posix()}, which the reader does not follow"
        )
    return None


def as_named(path: Path, tree: Path) -> Path | None:
    """`path` made absolute as the system opens it, keeping the links it is named through: normalized as text, but for
    a path holding a `..`, which the system takes only after following any link before it, the part up to its last
    `..` is followed as the system follows it, so no `..` steps back out of a link as text would, and the names after
    it stay as named. The part inside `tree` is followed by `followed_named`, which stats nothing outside it, and is
    None when a link or `..` on it leads out, or a name before a `..` is not there; a part named outside the tree, or
    through a link above it, is resolved."""
    parts = path.absolute().parts
    if ".." not in parts:
        return lexical_absolute(path)
    last = len(parts) - 1 - parts[::-1].index("..")
    prefix = Path(*parts[: last + 1])
    inside, refused = followed_named(prefix, tree)
    if refused is not None:
        return None
    return (inside or given_path(prefix)).joinpath(*parts[last + 1 :])


def through_link(root: Path, names: Sequence[str], reader: TreeReader) -> bool:
    """Whether the file `names` name below `root` may lie through a link: a folder on its way, or the file itself, is a
    link, a junction, or another reparse point, judged from each name's own metadata and never followed, so nothing is
    stat through a link that may lead outside the examined tree. A name whose metadata cannot be read ends the look as
    one: what lies past it is not stat either. A name that is not there ends it as none, since nothing lies past it to
    reach."""
    folder = root
    for name in names:
        folder = folder / name
        try:
            if reader.is_link(folder):
                return True
        except (FileNotFoundError, NotADirectoryError):
            return False
        except OSError:
            return True
    return False


def named_below(root: Path, path: Path, tree: Path) -> tuple[str, ...] | None:
    """The names leading from `root`, a resolved folder, down to `path` as `as_named` gives it, or None when it lies
    outside `root`, or a link or `..` on its way inside `tree` leads out of it. The shortest leading part of the named
    path that resolves to `root` stands for it, so a link above the root, such as a temporary folder's, still finds it,
    and a link below it stays a folder the path is named through. A leading part inside `tree` that a link leads out of
    it (`outside_link_refusal`) ends the look before it is resolved, as when `root` lies below the tree."""
    named = as_named(path, tree)
    if named is None:
        return None
    key = given_key(root)
    for depth in range(1, len(named.parts)):
        prefix = Path(*named.parts[:depth])
        if outside_link_refusal(prefix, tree) is not None:
            return None
        if given_key(given_path(prefix)) == key:
            return named.parts[depth:]
    return None


def unsearched(root: Path, narrowed: Path | None, passed_over: Sequence[NotEntered], tree: Path) -> str | None:
    """Why the search for projects under `root` did not find `narrowed`, a .csproj file below it: the first folder on
    its way down that the search skips, prunes, or did not enter, or else the bound the search stopped at. None when
    `narrowed` is no such file, or nothing in the search explains its absence. A path through a link on its way is
    never stat, since the link may lead outside the examined tree: its folder is named whether or not the file is
    there, as the search, which does not follow it either, cannot tell."""
    if narrowed is None or not ordinal_fold(narrowed.name).endswith(PROJECT_SUFFIX):
        return None
    names = named_below(root, narrowed, tree)
    reader = TreeReader(tree)
    if not names or not (through_link(root, names, reader) or regular_file(narrowed)):
        return None
    skipped = {name.casefold() for name in SEARCH_SKIPPED_FOLDERS}
    not_entered = {reader.key(entry.path): entry for entry in passed_over}
    folder = root
    for name in names[:-1]:
        folder = folder / name
        entry = not_entered.get(reader.key(folder))
        if name.casefold() in skipped:
            reason = SKIPPED_REASON
        elif is_hidden(folder):
            reason = HIDDEN_REASON
        elif entry is not None:
            reason = entry.reason
        else:
            continue
        return (
            f"{narrowed} lies in {folder}, which the search for projects under {root} does not enter: {reason}; "
            f"{RETRY_ADVICE}"
        )
    bound = next((entry for entry in passed_over if entry.kind == BOUNDED), None)
    if bound is None:
        return None
    return (
        f"{narrowed} lies past the search for projects under {root}, which stopped at {bound.path}: {bound.reason}; "
        f"{RETRY_ADVICE}"
    )


# The reads made of what the user names, before a tree is bound or outside it, which ejryan89-skill-core's tree_reader
# does not judge: the root, the project, and the graph file, and the NuGet cache folders the environment names.
TREE_READ_ALLOWED = {
    "given_path": (
        "a --root, a --project, a NuGet cache folder, or the graph file the user names or the skill wrote, resolved "
        "with its links followed to find where it lies, before any tree or reader is bound to it, or to name the file "
        "written"
    ),
    "given_key": (
        "a --project's leading parts and the root's, keyed as the file system compares them to find the root among "
        "them, before any of them is read"
    ),
    "regular_file": (
        "whether a --root, a solution in it, or a --project the user names is a regular file, after as_given and "
        "linked_root judged its links inside its checkout"
    ),
    "root_reason": "why the --root the user names is no solution or folder, after as_given judged its links",
    "nuget_cache": (
        "whether each NuGet cache folder the environment and the home name is a folder, through any link to it, "
        "before it is resolved and a reader is bound to it (packages_reader)"
    ),
    "write_graph": "the mode of the graph file the user names with --output, which the skill replaces",
}


def given_path(path: Path) -> Path:
    """A path the user names, or the graph file the skill wrote, made absolute with every link followed."""
    return resolved_path(path)


def given_key(path: Path) -> str:
    """A path the user names as the file system compares it, `dotnet_solution.same_path`, before it is read."""
    return same_path(path)


def root_reason(path: Path) -> str | None:
    """`file_presence.refusal_reason` of the --root the user names, as a folder."""
    return file_presence.refusal_reason(path, folder=True)


def unexaminable(path: Path, error: OSError | Unreadable) -> Failed:
    """The refusal of a path the run could not examine, naming that path whatever file the error names, or none, with
    the operating system's reason, else the error's text, in the words `file_presence.refusal_reason` gives."""
    reason = error.reason if isinstance(error, Unreadable) else error.strerror or str(error)
    return Failed(f"{path} cannot be examined: {reason}")


def checkout_tree(base: Path) -> Path:
    """`examined_tree` of `base`, or `unexaminable` naming a checkout marker above it whose stat fails."""
    try:
        return examined_tree(base)
    except Unreadable as error:
        raise unexaminable(error.file, error) from None


def regular_file(path: Path) -> bool:
    """Whether a regular file is at `path`, False for nothing or something else there, and `unexaminable` naming the
    path when its stat fails."""
    try:
        return file_presence.is_present(path, other_is_absent=True)
    except OSError as error:
        raise unexaminable(path, error) from None


def root_solutions(folder: Path) -> list[Path]:
    """The solution files directly in `folder`, in path order: entries named as a solution that are regular files,
    so a folder so named is passed over. A folder it cannot list, or a solution-named entry it cannot stat, is refused
    naming it, never read as holding no solution, which would take the projects below it for the answer; so is one a
    link leads outside the checkout holding the folder (`linked_solution`), which is never stat."""
    tree = checkout_tree(folder)
    try:
        children = TreeReader(tree).listing(folder)
    except OSError as error:
        raise unexaminable(folder, error) from None
    return [child for child in children if is_solution(child) and not linked_root(child, tree) and regular_file(child)]


def linked_root(path: Path, tree: Path) -> bool:
    """False for a root, a solution or a folder, that no link on its way inside `tree` leads out of it
    (`outside_link_refusal`), and `Failed` naming it otherwise, since the root chooses every project the run reads and
    is never read from outside."""
    if (linked := outside_link_refusal(path, tree)) is not None:
        raise unexaminable(path, Unreadable(path, linked))
    return False


def as_given(path: Path, named: Path | None = None) -> Path:
    """`path` made absolute as the system opens it, judged as the root is (`resolve_root`): a path holding no `..` is
    normalized as text, keeping the links it is named through. One holding a `..`, which the system takes only after
    following any link before it, is followed inside the checkout holding its part before the first `..`
    (`examined_tree`) by `followed_named`, as `as_named` follows a --project, so a link inside the checkout that leads
    out of it, before a `..` or after it, fails naming the link, and a name not there before a `..` fails as missing.
    A `..` that climbs out of the checkout at its top, with no link before it, leaves it for the folder above where the
    checkout lies, resolved, which follows only links above every checkout, and the rest is judged again from there.
    A `..` after a link is so taken after following the link on every platform, never by the system's own resolving,
    so on Windows, which takes a path's own `..` by name, a path it would take past a link can be refused. A refusal
    names `named`, the path as the user gave it, else `path`."""
    absolute = path.absolute()
    named = named or absolute
    parts = absolute.parts
    if ".." not in parts:
        return lexical_absolute(path)
    tree = checkout_tree(Path(*parts[: parts.index("..")]))
    for index, name in enumerate(parts):
        if name != "..":
            continue
        inside, refused = followed_named(Path(*parts[: index + 1]), tree)
        if refused is not None:
            raise root_refusal(named, refused)
        if inside is None:
            return as_given(given_path(tree).parent.joinpath(*parts[index + 1 :]), named)
    inside, refused = followed_named(absolute, tree)
    if refused is not None:
        raise root_refusal(named, refused)
    return inside or absolute


def root_refusal(named: Path, refused: str) -> Failed:
    """The failure of a --root `followed_named` refuses: missing, as the system finds it, or not examined, with why."""
    if refused == file_presence.MISSING:
        return Failed(f"{named} {refused}")
    return unexaminable(named, Unreadable(named, refused))


def resolve_root(
    path: Path, graph_unreadable: list[tuple[Path, str]], passed_over: list[NotEntered] | None = None
) -> tuple[Path, Path, list[Path]]:
    """The solution or directory the graph is built from, the directory paths are shown relative to, and the
    project files to read: a solution's listed .csproj files, the one solution in a directory, or the .csproj files
    under a directory holding none. A solution that cannot be read is reported unreadable and lists no project, so
    the answer says why it is empty, and an entry `refusal` refuses is reported before anything looks it up.

    `path` is judged as named before it is stat or resolved: the checkout holding it is found from the top down
    (`examined_tree`), following a link on its way only while no checkout lies above it, so a link above the checkout
    is the user's own choice of root and is followed, while a solution or folder that a link inside the checkout leads
    out of it fails naming the link, as a solution in the root folder so linked does, and nothing is stat through it."""
    named = as_given(path)
    linked_root(named, checkout_tree(named.parent if is_solution(named) else named))
    path = given_path(named)
    if regular_file(path):
        if not is_solution(path):
            raise Failed(f"{path} is not a .sln or .slnx solution")
        projects: list[Path] = []
        tree = checkout_tree(path.parent)
        reader = TreeReader(tree)
        try:
            entries = read_solution(path, reader)
        except Unreadable as error:
            graph_unreadable.append((error.file, error.reason))
            entries = []
        for listed in entries:
            if not ordinal_fold(listed).endswith(PROJECT_SUFFIX):
                continue
            candidate = project_path(path.parent, listed)
            refused = refusal(candidate, tree) or outside_link_refusal(candidate, tree)
            if refused is not None:
                graph_unreadable.append((candidate, refused))
                continue
            try:
                present = reader.there(candidate)
            except OSError as error:
                graph_unreadable.append((candidate, error.strerror or str(error)))
                continue
            if present:
                projects.append(candidate)
            else:
                graph_unreadable.append((candidate, "listed by the solution but not found"))
        return path, path.parent, projects
    # Neither a file nor a folder: missing, something else, or a path a stat cannot examine, each named so.
    if reason := root_reason(path):
        raise Failed(f"{path} {reason}")
    solutions = root_solutions(path)
    if len(solutions) > 1:
        names = ", ".join(solution.name for solution in solutions)
        raise Failed(f"{path} holds more than one solution ({names}); name one with --root")
    if solutions:
        return resolve_root(solutions[0], graph_unreadable)
    return path, path, find_projects(path, graph_unreadable, passed_over)


def no_projects(
    directory: Path,
    narrowed: Path | None,
    unreadable: Iterable[tuple[Path, str]],
    passed_over: Sequence[NotEntered],
    tree: Path,
) -> Failed:
    """The failure of a search for projects that found none: why it did not reach `narrowed`, the project `--project`
    names, when `unsearched` finds why, else that it found none, either way naming each folder it did not search."""
    skipped = "".join(f"; {folder} not searched: {reason}" for folder, reason in unreadable)
    reason = unsearched(directory, narrowed, passed_over, tree)
    return Failed(f"{reason or f'no solution or .csproj found under {directory}'}{skipped}")


def load_projects(graph: Graph, listed: Iterable[Path]) -> None:
    """Read each listed project and, through ProjectReference, every project they reach under any framework, so a
    project one framework removes is still read for the others. A referenced project `refusal` refuses is reported
    and never read, so no search for the files above a project starts from its folder."""
    queue = deque((path, True) for path in listed)
    while queue:
        path, is_listed = queue.popleft()
        key = graph.key(path)
        if key in graph.projects:
            continue
        try:
            refused = refusal(path, graph.tree)
            if refused is not None:
                raise Unreadable(path, refused)
            file = read_project(path, graph.sources or Sources(graph.tree))
        except Unreadable as error:
            graph.refuse(error)
            placeholder = ProjectFile(path, "legacy", path.stem, [], [], [], [])
            graph.projects[key] = Project(placeholder, is_listed, readable=False)
            continue
        graph.projects[key] = Project(file, is_listed)
        for report in file.reports:
            graph.refuse(report)
        queue.extend((reference, False) for reference in file.project_refs)


def read_configs(graph: Graph, project: Project) -> None:
    """The binding redirects of the project's web.config and app.config, when it has them. A project folder it cannot
    list, or a config it cannot stat, is reported against itself, and the rest of the graph is still answered."""
    folder = project.file.path.parent
    try:
        entries = graph.reader.listing(folder)
    except OSError as error:
        graph.refuse(os_unreadable(error, folder))
        return
    for candidate in entries:
        if ordinal_fold(candidate.name) not in CONFIG_FILES:
            continue
        if (linked := outside_link_refusal(candidate, graph.tree)) is not None:
            graph.refuse(Unreadable(candidate, linked))
            continue
        try:
            if not graph.reader.present(candidate, other_is_absent=True):
                continue
            project.file.redirects += read_redirects(candidate, graph.reader)
        except OSError as error:
            graph.refuse(os_unreadable(error, candidate))
        except Unreadable as error:
            graph.refuse(error)


def nuget_cache() -> list[Path]:
    """The folders the NuGet cache may be in: NUGET_PACKAGES where it names an absolute path, and ~/.nuget/packages,
    each that is a folder resolved with its links followed, so a cache named through a link is read where it lies and
    only a link inside it that leads out of it is refused (`packages_reader`); `Unreadable` naming one whose stat
    fails, and `Failed` naming the home's variable where the home is not an absolute path, so no cache is looked for
    under the working directory."""
    folders: list[Path] = []
    configured = environment_path("NUGET_PACKAGES")
    if configured is not None:
        folders.append(configured)
    try:
        folders.append(absolute_home() / ".nuget" / "packages")
    except RelativeEnvironmentPath as error:
        raise Failed(f"{error}, so the NuGet cache under the home folder is not known") from None
    found: list[Path] = []
    for folder in folders:
        try:
            if file_presence.is_directory(folder, other_is_absent=True):
                found.append(given_path(folder))
        except OSError as error:
            raise os_unreadable(error, folder) from None
    return found


def packages_folder(project_dir: Path, stop: Path, tree: Path) -> Path | None:
    """The nearest `packages` folder from the project's directory up to the solution's, or up to `tree`'s top when
    that comes first, so no `packages` folder above the tree is read; None when there is none, and `Unreadable` naming
    a candidate whose stat fails, or one a link leads outside `tree` (`outside_link_refusal`), which is never stat,
    and so does a project folder the climb cannot start from, one the reader refuses or whose key it cannot take."""
    reader = TreeReader(tree)
    try:
        folders = list(reader.climb(project_dir, stop))
    except OSError as error:
        raise os_unreadable(error, project_dir) from None
    for folder in folders:
        if (linked := outside_link_refusal(candidate := folder / "packages", tree)) is not None:
            raise Unreadable(candidate, linked)
        try:
            if reader.folder(candidate, other_is_absent=True):
                return candidate
        except OSError as error:
            raise os_unreadable(error, folder / "packages") from None
    return None


def packages_reader(folder: Path, tree: Path) -> TreeReader:
    """The reader a packages folder is read through: the examined tree's for a `packages` folder inside it, and one
    bound to the folder itself for a NuGet cache folder outside it, so no link is followed out of either."""
    return TreeReader(tree if within(folder, tree) else folder)


def package_directory(folders: Sequence[Path], package: str, version: str, tree: Path) -> Path | None:
    """The folder a package at a version is unpacked in: `<id>.<version>` in a packages folder, or `<id>/<version>` in
    the NuGet cache, matched without regard to case, each folder read through its own reader (`packages_reader`). A
    folder it cannot list or an entry it cannot stat raises Unreadable naming it, never reads as a package that is not
    unpacked there, and so does an entry that a link leads outside the reader's tree."""
    for folder in folders:
        reader = packages_reader(folder, tree)
        wanted = {ordinal_fold(f"{package}.{version}"), ordinal_fold(f"{package}/{version}")}
        for child in _entries(folder, reader):
            if ordinal_fold(child.name) == ordinal_fold(f"{package}.{version}") and _folder(child, reader):
                return child
            if ordinal_fold(child.name) == ordinal_fold(package) and _folder(child, reader):
                for inner in _entries(child, reader):
                    if ordinal_fold(f"{child.name}/{inner.name}") in wanted and _folder(inner, reader):
                        return inner
    return None


def _entries(folder: Path, reader: TreeReader) -> list[Path]:
    """What a folder holds, or Unreadable naming the folder when it cannot be listed or `reader` refuses it."""
    try:
        return reader.listing(folder)
    except OSError as error:
        raise os_unreadable(error, folder) from None


def _folder(path: Path, reader: TreeReader) -> bool:
    """Whether a folder is at path: False for nothing or something else there, and Unreadable naming the path when
    its stat fails or a link leads it outside the reader's tree, which is never stat."""
    if (refused := reader.refusal(path)) is not None:
        raise Unreadable(path, refused)
    try:
        return reader.folder(path, other_is_absent=True)
    except OSError as error:
        raise os_unreadable(error, path) from None


def nuspec_node(graph: Graph, folders: Sequence[Path], reference: PackageRef) -> PackageNode | None:
    """The package's node from its nuspec in one of the folders, or None when it is not unpacked there."""
    if not reference.version:
        return None
    try:
        directory = package_directory(folders, reference.id, reference.version, graph.tree)
    except Unreadable as error:
        graph.refuse(error)
        return None
    if directory is None:
        return None
    # A package is read through the reader of the packages folder, or the NuGet cache folder, it was found in.
    holder = next((folder for folder in folders if within(directory, folder)), directory)
    reader = packages_reader(holder, graph.tree)
    try:
        nuspecs = [
            path
            for path in reader.listing(directory)
            if ordinal_fold(path.name) == ordinal_fold(f"{reference.id}.nuspec")
        ]
    except OSError as error:
        graph.refuse(os_unreadable(error, directory))
        return None
    if not nuspecs:
        return None
    if (refused := reader.refusal(nuspecs[0])) is not None:
        graph.refuse(Unreadable(nuspecs[0], refused))
        return None
    try:
        return read_nuspec(nuspecs[0], reader, package_assemblies(directory, reader))
    except Unreadable as error:
        graph.refuse(error)
        return None


def file_target(project: Project, direct: list[PackageRef]) -> Target:
    """The one target of a project with no restore: the direct packages, and the project and assembly references of
    the view its builds give together (`ProjectFile`), every Include and Update that may apply in one, but those a
    Remove takes out in every one; a Remove that cannot be settled in one and fails in none is reported
    (`refuse_file_unevaluated`)."""
    file = project.file
    return Target(NO_FRAMEWORK, direct=direct, project_refs=list(file.project_refs), assemblies=list(file.assemblies))


def closure_from_nuspecs(graph: Graph, project: Project, direct: list[PackageRef], folders: Sequence[Path]) -> None:
    """One target of the direct packages with their nodes from their nuspecs; edges are `nuspec` when any was
    found."""
    target = file_target(project, direct)
    for reference in direct:
        node = nuspec_node(graph, folders, reference)
        if node is not None:
            target.nodes[ordinal_fold(reference.id)] = node
    project.targets = [target]
    project.edges = "nuspec" if target.nodes else "direct"


PACKAGE_KINDS = ("PackageReference", "PackageVersion", "GlobalPackageReference")


def refuse_file_unevaluated(graph: Graph, project: Project, packages: bool) -> None:
    """Report what the one target of a project with no restore leaves unsettled (`file_target`): each Remove kept
    because it cannot be settled in a build and fails in none, and each item whose name cannot be settled, of its
    project and assembly references, and of its packages unless a packages.config gives them."""
    kinds = ("ProjectReference", "Reference", *(PACKAGE_KINDS if packages else ()))
    graph.refuse_unsettled(project, project.file.unsettled, kinds)


def direct_references(graph: Graph, project: Project, assets: Assets, framework: str) -> list[PackageRef]:
    """A framework's direct packages: those its restore recorded, an empty list included, else those its build
    leaves, at the central versions it leaves, each item it cannot settle reported and not applied."""
    if framework in assets.direct:
        return assets.direct[framework]
    build = graph.build_of(project, framework)
    graph.refuse_unsettled(project, build.unsettled, PACKAGE_KINDS)
    return build.packages


def project_references(graph: Graph, project: Project, assets: Assets, framework: str) -> list[Path]:
    """A framework's project references: the project file's that its restore recorded for it, else those its build
    leaves, each item it cannot settle reported and not applied."""
    recorded = assets.project_refs.get(framework)
    if recorded is not None:
        keys = {graph.key(path) for path in recorded}
        return [path for path in project.file.project_refs if graph.key(path) in keys]
    build = graph.build_of(project, framework)
    graph.refuse_unsettled(project, build.unsettled, ("ProjectReference",))
    return build.project_refs


def assembly_references(graph: Graph, project: Project, framework: str) -> list[AssemblyRef]:
    """A framework's assembly references, which a restore does not record: those its build leaves, each item it
    cannot settle reported and not applied."""
    build = graph.build_of(project, framework)
    graph.refuse_unsettled(project, build.unsettled, ("Reference",))
    return build.assemblies


def closure_from_assets(graph: Graph, project: Project, assets_path: Path, services: Services) -> None:
    """One target per framework the assets file restored, each with its own packages and direct references; the
    project keeps its direct target when the file cannot be read or restored no framework."""
    try:
        assets = read_assets(assets_path, graph.reader, project.file.frameworks)
    except Unreadable as error:
        graph.refuse(error)
        return
    if not assets.nodes:
        return
    project.targets = [
        Target(
            framework,
            {ordinal_fold(package): node for package, node in nodes.items()},
            direct_references(graph, project, assets, framework),
            project_references(graph, project, assets, framework),
            assembly_references(graph, project, framework),
        )
        for framework, nodes in assets.nodes.items()
    ]
    project.edges = "assets"
    try:
        fresh = graph.reader.status(assets_path).st_mtime >= graph.reader.status(project.file.path).st_mtime
    except OSError as error:
        # Whether the restore is older than the project cannot be told, so the SDK is not asked to answer for it.
        graph.refuse(os_unreadable(error, assets_path))
        fresh = False
    if fresh and services.which("dotnet") is not None:
        # The SDK reads what sdk_withheld judges, so a project it withholds is never asked about and the walk answers it
        # alone.
        project.sdk_withheld = sdk_withheld(graph, project)
        project.sdk_answers = project.sdk_withheld is None


def sdk_withheld(graph: Graph, project: Project) -> str | None:
    """Why `dotnet nuget why` is not asked about a restored project, None when it is: the .NET host and MSBuild would
    read or stat a file the reader cannot judge to lie inside the examined tree. The first file the project's
    evaluation refused for where it lies, else the first it could not place (`ProjectFile.unplaced`), else the nearest
    global.json (`global_json_withheld`)."""
    refused = project.file.refused()
    if refused:
        return SDK_NOT_ASKED.format(file=graph.relative(refused[0]))
    unplaced = project.file.unplaced()
    if unplaced:
        return unplaced_reason(graph, unplaced[0])
    return global_json_withheld(graph, project)


def unplaced_reason(graph: Graph, entry: UnplacedImport) -> str:
    template = SDK_UNPLACED_GENERATED if entry.generated else SDK_UNPLACED
    return template.format(written=entry.written, file=graph.relative(entry.file))


def global_json_withheld(graph: Graph, project: Project) -> str | None:
    """Why the nearest global.json keeps the SDK from a project, None when it does not. The SDK runs in the project's
    folder (`sdk_why.run_why`), so the .NET host, which picks the SDK and its resolvers by the nearest global.json at or
    above the folder it runs in, and MSBuild's resolvers, which search from the project's folder, read the one this
    search finds, as `nearest_file` searches: kept from the SDK when it lies outside the examined tree, a link on its
    way leads out of it, or a candidate cannot be stat."""
    try:
        found = nearest_file(project.file.path.parent, GLOBAL_JSON, graph.reader)
    except Refused as error:
        return SDK_NOT_ASKED.format(file=graph.relative(error.file))
    except Unreadable as error:
        return SDK_NOT_JUDGED.format(file=graph.relative(error.file), reason=error.reason)
    if found is not None and refusal(found, graph.tree) is not None:
        return SDK_NOT_ASKED.format(file=graph.relative(found))
    return None


def build_closure(graph: Graph, project: Project, services: Services) -> None:
    """Fill the project's targets: the direct packages, their package nodes, and where their edges came from."""
    config = project.file.path.parent / PACKAGES_CONFIG
    if _present(graph, config):
        direct: list[PackageRef] = []
        try:
            direct = read_packages_config(config, graph.reader)
        except Unreadable as error:
            graph.refuse(error)
        folders: list[Path] = []
        try:
            folder = packages_folder(project.file.path.parent, graph.base, graph.tree)
            folders = [folder] if folder else nuget_cache()
        except Unreadable as error:
            graph.refuse(error)
        closure_from_nuspecs(graph, project, direct, folders)
        refuse_file_unevaluated(graph, project, packages=False)
        return
    project.targets = [file_target(project, list(project.file.packages))]
    assets_path = project.file.path.parent / "obj" / ASSETS_FILE
    if project.file.style == "sdk" and _present(graph, assets_path):
        closure_from_assets(graph, project, assets_path, services)
    if project.targets[0].framework == NO_FRAMEWORK:
        refuse_file_unevaluated(graph, project, packages=True)


def _present(graph: Graph, path: Path) -> bool:
    """Whether a regular file is at path: False for nothing or something else there, and for a path whose stat fails
    or that a link on its way leads outside the examined tree (`outside_link_refusal`), never stat, each reported
    against it, so the project is still answered from what can be read."""
    if (linked := outside_link_refusal(path, graph.tree)) is not None:
        graph.refuse(Unreadable(path, linked))
        return False
    try:
        return graph.reader.present(path, other_is_absent=True)
    except OSError as error:
        graph.refuse(os_unreadable(error, path))
        return False


def build_graph(root: Path, services: Services, narrowed: Path | None = None) -> Graph:
    """Read the solution's projects and, for each one whose file could be read, its packages and redirects. A search
    for projects that finds none fails as `no_projects` says."""
    unreadable: list[tuple[Path, str]] = []
    passed_over: list[NotEntered] = []
    resolved, base, listed = resolve_root(root, unreadable, passed_over)
    # resolve_root shows a solution's paths relative to its folder and a searched folder's relative to itself, so the
    # root is a solution exactly when it is not the base; a second look at the disk could answer otherwise.
    solution = None if resolved == base else resolved
    tree = checkout_tree(base)
    if solution is None and not listed:
        raise no_projects(resolved, narrowed, unreadable, passed_over, tree)
    graph = Graph(resolved, base, tree, unreadable=unreadable, passed_over=passed_over, sources=Sources(tree, solution))
    load_projects(graph, listed)
    for project in graph.projects.values():
        if project.readable:
            build_closure(graph, project, services)
            read_configs(graph, project)
    return graph


def print_graph(graph: Graph, out: Emitter) -> None:
    out.line("ROOT", graph.root.as_posix())
    for project in sorted(graph.projects.values(), key=lambda item: ordinal_fold(graph.label(item))):
        out.line(
            "PROJECT",
            graph.label(project),
            project.file.style,
            project.edges,
            "listed" if project.listed else "referenced",
        )
    for file, reason in graph.unreadable:
        out.line("UNREADABLE", graph.relative(file), reason)


def hop_text(hop: Hop) -> str:
    kind, name, version = hop
    if kind == "package":
        return f"{name} (v{version})" if version else f"{name} (v?)"
    if kind == "project":
        return f"{name} (project)"
    return f"{name} (v{version}, {kind})" if version else f"{name} ({kind})"


def package_hop(target: Target, package: str, requested: str | None) -> Hop:
    """A package hop with the version the target resolved, else the version it asked for."""
    node = target.node(package)
    return ("package", node.id if node else package, node.version if node else requested or "")


def assembly_hop(name: str, version: str | None, hint_path: str | None) -> Hop:
    return ("assembly" if hint_path else "framework", name, version or "")


def labelled(rows: Iterable[tuple[str, Row]]) -> list[tuple[str, Row]]:
    """Each distinct row once, in the order first met, with the frameworks that hold it joined by `;`."""
    merged: dict[Row, list[str]] = {}
    for framework, row in rows:
        frameworks = merged.setdefault(row, [])
        if framework not in frameworks:
            frameworks.append(framework)
    return [(";".join(frameworks), row) for row, frameworks in merged.items()]


class PathFinder:
    """Every chain from a project to a name under one framework, through the framework's project references, direct
    packages, and assembly references, package edges, and the assemblies a package ships. A referenced project is
    walked under the same framework when it has it, else under each of its own."""

    def __init__(self, graph: Graph, target: str, framework: str, limit: int | None = None) -> None:
        self.graph = graph
        self.target = ordinal_fold(target)
        self.framework = framework
        self.limit = PATH_LIMIT if limit is None else limit
        self.found: list[tuple[Hop, ...]] = []
        self.truncated = False

    def record(self, path: tuple[Hop, ...]) -> None:
        if path in self.found:
            return
        if len(self.found) < self.limit:
            self.found.append(path)
        else:
            self.truncated = True

    def from_project(self, project: Project, path: tuple[Hop, ...], seen: frozenset[str]) -> None:
        key = self.graph.key(project.file.path)
        if key in seen:
            return
        seen = seen | {key}
        projects, assemblies = project.references_for(self.framework, self.graph.key)
        for reference in projects:
            child = self.graph.project_at(reference)
            if child is not None:
                hop: Hop = ("project", child.file.assembly_name, "")
                if (
                    ordinal_fold(child.file.assembly_name) == self.target
                    or ordinal_fold(child.file.path.stem) == self.target
                ):
                    self.record((*path, hop))
                self.from_project(child, (*path, hop), seen)
        for target in project.targets_for(self.framework):
            for direct in target.direct:
                self.from_package(target, direct.id, direct.version, path, frozenset())
        for assembly in assemblies:
            if ordinal_fold(assembly.name) == self.target:
                self.record((*path, assembly_hop(assembly.name, assembly.version, assembly.hint_path)))

    def from_package(
        self, owner: Target, package: str, requested: str | None, path: tuple[Hop, ...], seen: frozenset[str]
    ) -> None:
        fold = ordinal_fold(package)
        if fold in seen:
            return
        path = (*path, package_hop(owner, package, requested))
        if fold == self.target:
            self.record(path)
            return
        node = owner.node(package)
        if node is None:
            return
        shipped = next((name for name in node.assemblies if ordinal_fold(name) == self.target), None)
        if shipped is not None:
            self.record((*path, ("assembly", shipped, "")))
        for dependency, version in node.dependencies.items():
            self.from_package(owner, dependency, version, path, seen | {fold})


@dataclass(frozen=True)
class Reached:
    """Something a project reaches, with how it was first reached."""

    kind: str
    name: str
    version: str
    first_hop: str


Step = tuple[Hop, Target | None, Project | None]  # the hop, the target a package hop is resolved in, the project


def reaches(graph: Graph, project: Project, framework: str) -> list[Reached]:
    """Everything the project reaches under one framework, breadth first, each name once per version, with the
    direct reference that brought it in first. A package's edges are its owner project's, so a package met again
    through another project is walked again there: the two projects may resolve it to different versions."""
    found: dict[tuple[str, str, str], Reached] = {}
    walked_projects = {graph.key(project.file.path)}
    walked_packages: set[tuple[int, str]] = set()  # the target's identity and the package's case-folded id
    queue: deque[tuple[Step, str]] = deque()

    def own_steps(owner: Project) -> list[Step]:
        steps: list[Step] = []
        projects, assemblies = owner.references_for(framework, graph.key)
        for reference in projects:
            child = graph.project_at(reference)
            if child is not None:
                steps.append((("project", child.file.assembly_name, ""), None, child))
        for target in owner.targets_for(framework):
            steps += [(package_hop(target, direct.id, direct.version), target, None) for direct in target.direct]
        steps += [(assembly_hop(item.name, item.version, item.hint_path), None, None) for item in assemblies]
        return steps

    def enqueue(steps: Iterable[Step], first: str | None) -> None:
        queue.extend((step, first or hop_text(step[0])) for step in steps)

    enqueue(own_steps(project), None)
    while queue:
        (hop, target, child), first = queue.popleft()
        kind, name, version = hop
        found.setdefault(
            (kind if kind != "framework" else "assembly", ordinal_fold(name), version),
            Reached(kind, name, version, first),
        )
        if child is not None and graph.key(child.file.path) not in walked_projects:
            walked_projects.add(graph.key(child.file.path))
            enqueue(own_steps(child), first)
        elif target is not None and (id(target), ordinal_fold(name)) not in walked_packages:
            walked_packages.add((id(target), ordinal_fold(name)))
            node = target.node(name)
            if node is not None:
                enqueue(
                    [(package_hop(target, dep, wanted), target, None) for dep, wanted in node.dependencies.items()],
                    first,
                )
    return list(found.values())


def named_project(graph: Graph, narrowed: Path) -> Project | None:
    """The project `--project` names: the one at the path as it is named below the solution's directory, where the
    search or the solution found a project, through a link to its file too, and keeps it under the path it found it at,
    else the one at the path it resolves to inside the examined tree. A path that a link or `..` on its way leads out
    of the tree (`as_named`, `outside_link_refusal`), or that is named outside it, is never resolved in full. A
    project found through a link is read from the link's folder, as MSBuild reads it, so the link's project, not its
    target's, is the one named."""
    names = named_below(graph.base, narrowed, graph.tree)
    project = graph.project_at(graph.base.joinpath(*names)) if names else None
    if project is not None:
        return project
    named = as_named(narrowed, graph.tree)
    if named is None or not within(named, graph.tree) or outside_link_refusal(named, graph.tree) is not None:
        return None
    return graph.project_at(given_path(narrowed))


def selected_projects(graph: Graph, narrowed: Path | None) -> list[Project]:
    """Every project in label order, or the one `--project` names. A project of no solution that a search for projects
    did not reach fails saying why, as `unsearched` finds it, so the run names the search's bound or the folder it
    passes over rather than the project."""
    if narrowed is None:
        return sorted(graph.projects.values(), key=lambda item: ordinal_fold(graph.label(item)))
    project = named_project(graph, narrowed)
    if project is None:
        searched = graph.root == graph.base
        reason = unsearched(graph.root, narrowed, graph.passed_over, graph.tree) if searched else None
        raise Failed(reason or f"{narrowed} is not a project of {graph.root}")
    return [project]


def sdk_answers(
    graph: Graph, projects: list[Project], name: str, services: Services, out: Emitter
) -> dict[str, list[SdkPath]]:
    """The SDK's chains per project it answered, keyed by the project's path; each failed run is printed, and so is each
    project the SDK would answer but is not asked about (`sdk_withheld`), with the reason it gives."""
    for project in projects:
        if project.sdk_withheld is not None:
            out.line("SDK_FAILED", graph.label(project), name, project.sdk_withheld)
    candidates = [project for project in projects if project.sdk_answers]
    if not candidates:
        return {}
    answer = run_why([project.file.path for project in candidates], name, services, DEFAULT_TIMEOUT_SECONDS)
    for project in candidates:
        failure = answer.failures.get(project.file.path)
        if failure:
            out.line("SDK_FAILED", graph.label(project), name, failure)
    return {
        graph.key(project.file.path): answer.paths[project.file.path]
        for project in candidates
        if project.file.path in answer.answered
    }


def same_route(walked: tuple[Hop, ...], printed: SdkPath) -> bool:
    """Whether a chain of the graph walk takes the route the SDK printed: the same packages and projects in order,
    whatever their versions. The SDK prints each package at the version the answering project resolves, and the
    walk takes a referenced project's packages from that project's own restore, which may resolve another."""
    return len(walked) == len(printed.hops) and all(
        kind in ("package", "project") and ordinal_fold(name) == ordinal_fold(sdk_name)
        for (kind, name, _), (sdk_name, _) in zip(walked, printed.hops, strict=True)
    )


def merged_chain(printed: SdkPath, walked: list[tuple[Hop, ...]]) -> str:
    """The SDK's chain, with the hops of the walk's chains along the same route when there are any, so a project hop
    reads alike from both: each package at the SDK's version, and the walk's own versions of it that differ named after
    `graph`, joined by `;` in the order the walk found them."""
    if not walked:
        return " > ".join(hop_text(("package", *hop)) for hop in printed.hops)
    hops: list[str] = []
    for position, (_, sdk_version) in enumerate(printed.hops):
        kind, hop_name, _ = walked[0][position]
        if kind == "project":
            hops.append(hop_text(walked[0][position]))
            continue
        versions = dict.fromkeys(chain[position][2] for chain in walked)
        apart = [version for version in versions if version not in ("", sdk_version)]
        graph_versions = f", graph {';'.join(f'v{version}' for version in apart)}" if apart else ""
        hops.append(f"{hop_name} (v{sdk_version}{graph_versions})")
    return " > ".join(hops)


def project_paths(graph: Graph, project: Project, name: str, sdk: list[SdkPath] | None, out: Emitter) -> int:
    """Print the chains from the project to the name, labelled with their frameworks; return how many lines. Each
    chain the SDK printed is named `sdk`, and holds every chain the walk found along the same route, so a route is
    printed once whichever found it (`merged_chain`); every chain only the walk found, such as one ending in an
    assembly or a referenced project, or one through a packages.config project, which the SDK cannot see, is named
    `graph`. Without the SDK's chains, the walk answers alone. At most `PATH_LIMIT` lines are printed, the SDK's
    first, and TRUNCATED names every framework a chain past the limit holds for, whichever found it."""
    walked: list[tuple[str, tuple[Hop, ...]]] = []
    truncated: list[str] = []
    for target in project.targets:
        finder = PathFinder(graph, name, target.framework)
        finder.from_project(project, (), frozenset())
        walked += [(target.framework, chain) for chain in finder.found]
        if finder.truncated:
            truncated.append(target.framework)
    rows: list[tuple[str, Row]] = []
    claimed: set[int] = set()
    for answer in sdk or []:
        framework = (
            matching_framework(answer.framework, (target.framework for target in project.targets))
            or answer.framework
            or NO_FRAMEWORK
        )
        matched = [
            index
            for index, (walked_framework, chain) in enumerate(walked)
            if index not in claimed and walked_framework == framework and same_route(chain, answer)
        ]
        claimed.update(matched)
        rows.append((framework, ("sdk", merged_chain(answer, [walked[index][1] for index in matched]))))
    rows += [
        (framework, ("graph", " > ".join(hop_text(hop) for hop in chain)))
        for index, (framework, chain) in enumerate(walked)
        if index not in claimed
    ]
    printed = labelled(rows)
    for frameworks, _ in printed[PATH_LIMIT:]:
        truncated += [framework for framework in frameworks.split(";") if framework not in truncated]
    printed = printed[:PATH_LIMIT]
    for frameworks, (source, chain) in printed:
        out.line("PATH", graph.label(project), frameworks, name, source, chain)
    if truncated:
        out.line("TRUNCATED", graph.label(project), ";".join(truncated), name, PATH_LIMIT)
    return len(printed)


def why_query(graph: Graph, names: list[str], narrowed: Path | None, services: Services, out: Emitter) -> None:
    projects = selected_projects(graph, narrowed)
    paths, found, missing = 0, 0, 0
    for name in names:
        answered = sdk_answers(graph, projects, name, services, out)
        count = sum(
            project_paths(graph, project, name, answered.get(graph.key(project.file.path)), out) for project in projects
        )
        if count:
            found += 1
        else:
            out.line("MISSING", name)
            missing += 1
        paths += count
    out.line("SUMMARY", paths, found, missing)


def why_all_query(graph: Graph, narrowed: Path, out: Emitter) -> None:
    (project,) = selected_projects(graph, narrowed)
    rows = labelled(
        (target.framework, (item.name, item.version or "-", item.kind, item.first_hop))
        for target in project.targets
        for item in reaches(graph, project, target.framework)
    )
    for frameworks, (name, version, kind, first_hop) in sorted(
        rows, key=lambda row: (row[1][2], ordinal_fold(row[1][0]), row[1][1])
    ):
        out.line("REACHES", graph.label(project), frameworks, name, version, kind, first_hop)
    out.line("SUMMARY", len(rows))


def resolved_assemblies(graph: Graph, project: Project, target: Target) -> list[tuple[str, str, str, str]]:
    """Each assembly the project's references resolve to under one target: name, kind, version, and what brings it
    in. A package's assemblies are those its restore or its unpacked folder shows, else the package id; the package
    closure is the target's own (a restore records what flows in through project references too), with the first
    hop that brings each package in where the graph walk finds one."""
    found: list[tuple[str, str, str, str]] = []
    for assembly in target.assemblies:
        kind = "reference" if assembly.hint_path else "framework"
        found.append((assembly.name, kind, assembly.version or "-", assembly.hint_path or "-"))
    first_hops = {
        (ordinal_fold(item.name), item.version): item.first_hop
        for item in reaches(graph, project, target.framework)
        if item.kind == "package"
    }
    packages: dict[str, tuple[str, str]] = {
        ordinal_fold(node.id): (node.id, node.version) for node in target.nodes.values()
    }
    for direct in target.direct:
        packages.setdefault(ordinal_fold(direct.id), (direct.id, direct.version or ""))
    for fold, (package, version) in packages.items():
        node = target.node(package)
        names = node.assemblies if node and node.assemblies else (package,)
        via = first_hops.get((fold, version)) or hop_text(("package", package, version))
        found += [(name, "package", version or "-", via) for name in names]
    return found


def where_query(graph: Graph, assembly: str, narrowed: Path | None, out: Emitter) -> None:
    target_name = ordinal_fold(assembly)
    projects = 0
    for project in selected_projects(graph, narrowed):
        rows: list[tuple[str, Row]] = []
        for target in project.targets:
            rows += [
                (target.framework, (name, kind, version, via))
                for name, kind, version, via in resolved_assemblies(graph, project, target)
                if ordinal_fold(name) == target_name
            ]
            for reference in target.project_refs:
                child = graph.project_at(reference)
                if child is not None and ordinal_fold(child.file.assembly_name) == target_name:
                    rows.append((target.framework, (child.file.assembly_name, "project", "-", graph.label(child))))
        for frameworks, row in labelled(rows):
            out.line("WHERE", graph.label(project), frameworks, *row)
        projects += bool(rows)
    out.line("SUMMARY", projects)


@dataclass(frozen=True)
class Want:
    name: str
    kind: str
    version: str
    project: str
    framework: str
    via: str


def wants(graph: Graph, project: Project) -> list[Want]:
    """Every package and assembly version the project wants under each of its frameworks, directly or through what
    it reaches."""
    label = graph.label(project)
    found: list[Want] = []
    for target in project.targets:
        found += [
            Want(item.name, "package", item.version, label, target.framework, item.first_hop)
            for item in reaches(graph, project, target.framework)
            if item.kind == "package" and item.version
        ]
        found += [
            Want(
                assembly.name, "assembly", assembly.version, label, target.framework, assembly.hint_path or "reference"
            )
            for assembly in target.assemblies
            if assembly.version
        ]
    return found


def at_odds(items: list[Want]) -> bool:
    """Whether two wants of one name differ in version where one build may load both: in two projects, or in one
    project under one framework, which its wants name by one target's name. Two frameworks of one project are built
    apart."""
    return any(
        first.version != second.version and (first.project != second.project or first.framework == second.framework)
        for first, second in combinations(items, 2)
    )


def redirect_lines(graph: Graph, project: Project, out: Emitter) -> int:
    """Check each binding redirect against the assembly versions the project references; return the stale count."""
    stale = 0
    referenced = {
        ordinal_fold(name)
        for target in project.targets
        for name, _, _, _ in resolved_assemblies(graph, project, target)
    }
    _, assemblies = references_of(project.targets, graph.key)
    for redirect in project.file.redirects:
        fold = ordinal_fold(redirect.name)
        versions = sorted({item.version for item in assemblies if ordinal_fold(item.name) == fold and item.version})
        config = graph.relative(redirect.config)
        if versions and redirect.new_version not in versions:
            out.line(
                "REDIRECT_STALE", graph.label(project), config, redirect.name, redirect.new_version, ",".join(versions)
            )
            stale += 1
        elif not versions and fold not in referenced:
            out.line("REDIRECT_UNREFERENCED", graph.label(project), config, redirect.name, redirect.new_version)
    return stale


def conflicts_query(graph: Graph, narrowed: Path | None, out: Emitter) -> None:
    projects = selected_projects(graph, narrowed)
    by_name: dict[tuple[str, str], list[Want]] = {}
    for project in projects:
        for want in wants(graph, project):
            by_name.setdefault((want.kind, ordinal_fold(want.name)), []).append(want)
    names = 0
    for (kind, _), items in sorted(by_name.items(), key=lambda entry: (entry[0][1], entry[0][0])):
        if not at_odds(items):
            continue
        names += 1
        rows = labelled((item.framework, (item.name, item.version, item.project, item.via)) for item in items)
        for frameworks, (name, version, label, via) in sorted(
            rows, key=lambda row: (row[1][1], ordinal_fold(row[1][2]))
        ):
            out.line("CONFLICT", name, kind, version, label, frameworks, via)
    stale = sum(redirect_lines(graph, project, out) for project in projects)
    out.line("SUMMARY", names, stale)


Nodes = dict[str, tuple[str, str]]


def package_key(target: Target, package: str, requested: str | None) -> tuple[str, str]:
    """A package node's key and label, at the version the target resolved or asked for."""
    _, name, version = package_hop(target, package, requested)
    return f"package:{ordinal_fold(name)}@{version}", f"{name} {version}".strip()


def with_references(graph: Graph, projects: list[Project]) -> list[Project]:
    """The projects and every project they reach through project references, in label order."""
    found = {graph.key(project.file.path): project for project in projects}
    queue = deque(projects)
    while queue:
        for reference in references_of(queue.popleft().targets, graph.key)[0]:
            child = graph.project_at(reference)
            if child is not None and graph.key(child.file.path) not in found:
                found[graph.key(child.file.path)] = child
                queue.append(child)
    return sorted(found.values(), key=lambda item: ordinal_fold(graph.label(item)))


def graph_edges(graph: Graph, projects: list[Project]) -> tuple[Nodes, list[tuple[str, str]]]:
    """Every node of the projects, keyed, with its label and kind, and every edge between keys, each once; a
    package's edges are drawn from every framework's target."""
    nodes: Nodes = {}
    edges: list[tuple[str, str]] = []

    def add(key: str, label: str, kind: str) -> str:
        nodes.setdefault(key, (label, kind))
        return key

    for project in projects:
        source = add(f"project:{graph.key(project.file.path)}", graph.label(project), "project")
        references, assemblies = references_of(project.targets, graph.key)
        for reference in references:
            child = graph.project_at(reference)
            if child is not None:
                edges.append((source, add(f"project:{graph.key(child.file.path)}", graph.label(child), "project")))
        for target in project.targets:
            edges.extend(
                (source, add(*package_key(target, direct.id, direct.version), "package")) for direct in target.direct
            )
            for node in target.nodes.values():
                owner = add(*package_key(target, node.id, node.version), "package")
                for dependency, wanted in node.dependencies.items():
                    edges.append((owner, add(*package_key(target, dependency, wanted), "package")))
        for assembly in assemblies:
            kind = "assembly" if assembly.hint_path else "framework"
            edges.append((source, add(f"{kind}:{ordinal_fold(assembly.name)}", assembly.name, kind)))
    return nodes, list(dict.fromkeys(edges))


DOT_SHAPES = {"project": "box", "package": "ellipse", "assembly": "note", "framework": "plaintext"}


def render_dot(nodes: Nodes, edges: list[tuple[str, str]]) -> str:
    ids = {key: f"n{index}" for index, key in enumerate(nodes)}
    lines = ["digraph dependencies {", "  rankdir=LR;"]
    for key, (label, kind) in nodes.items():
        escaped = label.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'  {ids[key]} [label="{escaped}" shape={DOT_SHAPES[kind]}];')
    lines += [f"  {ids[source]} -> {ids[target]};" for source, target in edges]
    return "\n".join([*lines, "}"]) + "\n"


def render_mermaid(nodes: Nodes, edges: list[tuple[str, str]]) -> str:
    ids = {key: f"n{index}" for index, key in enumerate(nodes)}
    lines = ["graph LR"]
    for key, (label, kind) in nodes.items():
        lines.append(f'  {ids[key]}["{label.replace(chr(34), "#quot;")}"]:::{kind}')
    lines += [f"  {ids[source]} --> {ids[target]}" for source, target in edges]
    lines += [f"  classDef {kind} stroke-width:{2 if kind == 'project' else 1}px" for kind in DOT_SHAPES]
    return "\n".join(lines) + "\n"


def refuse_output(requested: Path | None) -> None:
    """Refuse a requested graph file inside a skills directory, before the query runs."""
    owner = None if requested is None else skills_directory_holding(requested)
    if owner is not None:
        raise Failed(f"{requested} is inside the skills directory {owner}; choose a path outside it")


def write_graph(text: str, requested: Path | None, fmt: str) -> Path:
    """Write the graph to the requested path, else to a file in a new temporary directory named for the skill, made
    only once there is a graph to write and removed again when the write fails, so a failed query leaves nothing.

    The requested file is replaced whole through ejryan89-skill-core's `atomic_file`, keeping the permissions of a
    file already there, so a write that fails part way leaves that file as it was. A requested path whose stat fails
    for any reason but nothing being there is refused naming it, before anything is written."""
    if requested is not None:
        try:
            mode = stat.S_IMODE(requested.stat().st_mode)
        except FileNotFoundError:
            # Imported here, not in the import block, which another change reworks at the same time.
            from atomic_file import TRACKED_FILE_MODE

            mode = TRACKED_FILE_MODE
        except OSError as error:
            raise unexaminable(requested, error) from None
        replace_text(requested, text, mode=mode)
        return requested
    folder = Path(tempfile.mkdtemp(prefix="dotnet-dependencies-", dir=private_folder.temporary_directory()))
    target = folder / ("graph.dot" if fmt == "dot" else "graph.mmd")
    try:
        target.write_text(text, encoding="utf-8", newline="\n")
    except OSError:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return target


def graph_query(graph: Graph, narrowed: Path | None, fmt: str, requested: Path | None, out: Emitter) -> None:
    refuse_output(requested)
    nodes, edges = graph_edges(graph, with_references(graph, selected_projects(graph, narrowed)))
    text = render_dot(nodes, edges) if fmt == "dot" else render_mermaid(nodes, edges)
    target = write_graph(text, requested, fmt)
    out.line("WROTE", given_path(target).as_posix(), len(nodes), len(edges))
    out.line("SUMMARY", len(nodes), len(edges))


def parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", type=Path, default=Path(), help="a .sln, .slnx, or directory (default: .)")
    narrowing = argparse.ArgumentParser(add_help=False)
    narrowing.add_argument("--project", type=Path, help="narrow the query to this project file")
    parsed = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parsed.add_subparsers(dest="command", required=True)
    why = commands.add_parser("why", parents=[common, narrowing], help="paths from each project to each name")
    why.add_argument("names", nargs="*", help="package or assembly names")
    why.add_argument("--all", action="store_true", help="everything --project reaches, with the first hop")
    where = commands.add_parser("where", parents=[common, narrowing], help="projects resolving to an assembly")
    where.add_argument("assembly")
    commands.add_parser("conflicts", parents=[common, narrowing], help="versions and redirects at odds")
    diagram = commands.add_parser("graph", parents=[common, narrowing], help="write the graph as a diagram")
    diagram.add_argument("--format", choices=("dot", "mermaid"), default="mermaid")
    diagram.add_argument("--output", type=Path, help="the file to write (default: a new temporary directory)")
    return parsed


def run(arguments: argparse.Namespace, services: Services, out: Emitter) -> None:
    graph = build_graph(arguments.root, services, arguments.project)
    print_graph(graph, out)
    if arguments.command == "why" and arguments.all:
        why_all_query(graph, arguments.project, out)
    elif arguments.command == "why":
        why_query(graph, arguments.names, arguments.project, services, out)
    elif arguments.command == "where":
        where_query(graph, arguments.assembly, arguments.project, out)
    elif arguments.command == "conflicts":
        conflicts_query(graph, arguments.project, out)
    else:
        graph_query(graph, arguments.project, arguments.format, arguments.output, out)


def main(argv: Sequence[str] | None = None, services: Services | None = None) -> int:
    parsed = parser()
    arguments = parsed.parse_args(argv)
    if arguments.command == "why":
        if arguments.all and (arguments.names or arguments.project is None):
            parsed.error("--all takes --project and no names")
        if not arguments.all and not arguments.names:
            parsed.error("why takes at least one name, or --all with --project")
    out = Emitter(print)
    try:
        run(arguments, services or Services(), out)
    except (Failed, OSError) as error:
        print(f"FAILED {flat_text(str(error))}")
        return 1
    return 1 if out.findings else 0


if __name__ == "__main__":
    run_main(main, errors="replace")
