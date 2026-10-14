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
    UNREADABLE    <file> <reason>                            a file the readers refused, or a condition they
                  cannot evaluate; its facts are missing
    SDK_FAILED    <project> <name> <reason>                  `dotnet nuget why` failed; the graph walk answered
    PATH          <project> <frameworks> <name> <sdk|graph> <hop> > <hop> ...    one chain, each hop
                  `name (vversion)`, `name (project)`, `name (vversion, assembly)`, `name (assembly)` for an
                  assembly a package ships, or `name (vversion, graph vother)` for a package the SDK resolves at one
                  version and the walk at another; the source says whether `dotnet nuget why` answered or the walk did
    TRUNCATED     <project> <frameworks> <name> <limit>      more chains than the limit; the first ones printed
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
project with no restore, whose facts are not kept per framework. A line that holds for several frameworks is printed
once. Two frameworks of one project resolving different versions build apart and are not a conflict. A framework is
named as the project names it, whichever form its restore writes (`net8.0` or `.NETCoreApp,Version=v8.0`). A
framework's direct packages are those its restore recorded; for a framework the restore's project section does not
name, they are the project file's PackageReference items whose conditions hold for it, a Choose applying its first
When that holds, and an item whose condition the reader cannot evaluate is left out and reported as an UNREADABLE
line naming the condition.

`why` asks `dotnet nuget why` for a project that is SDK-style, uses PackageReference, has an obj/project.assets.json
no older than the project file, and only when dotnet is installed. A run that fails, even after printing part of an
answer, is an SDK_FAILED line. The graph walk runs for every project too: for a project the SDK answered, the walk's
chains along a route the SDK printed are printed once with it, named `sdk`, with the walk's hops at the SDK's
versions, and the walk's version of a package after `graph` where a referenced project's own restore resolves it
apart, a split `conflicts` reports; the walk adds every chain the SDK cannot see, such as one ending in an assembly
or one through a packages.config project; for every other project it answers alone. So whether dotnet is installed
changes which source a route names and which version leads where the two differ, never which routes are printed.
The script changes nothing on disk
but the graph file, runs no restore or build, and exits 1 when it printed a CONFLICT, REDIRECT_STALE, or UNREADABLE
line, else 0; a FAILED <reason> line and exit 1 when the root cannot be resolved, and a usage error exits 2.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from console import run_main
from dotnet_solution import is_solution, read_listed_projects, same_path
from flat_text import flat_text
from msbuild_reader import (
    ASSETS_FILE,
    CONFIG_FILES,
    PACKAGES_CONFIG,
    Assets,
    PackageNode,
    PackageRef,
    ProjectFile,
    Unreadable,
    central_versions_file,
    package_assemblies,
    packages_for,
    read_assets,
    read_central_versions,
    read_nuspec,
    read_packages_config,
    read_project,
    read_redirects,
    same_framework,
)
from sdk_why import DEFAULT_TIMEOUT_SECONDS, SdkPath, Services, run_why
from skill_roots import skills_directory_holding
from xml_reader import XmlRefused

PROJECT_SUFFIX = ".csproj"
IGNORED_DIRECTORIES = frozenset({".git", "bin", "obj", "node_modules", "packages", ".vs"})
PATH_LIMIT = 200
FINDING_KEYS = ("CONFLICT", "REDIRECT_STALE", "UNREADABLE")
NO_FRAMEWORK = "-"
Hop = tuple[str, str, str]  # kind (package, project, assembly, framework), name, version or ""
Row = tuple[str, ...]


class Failed(Exception):
    """The query could not run; the message says why."""


@dataclass
class Target:
    """One framework's package closure of a project: the restore's target framework, or `-` for a project with no
    restore, whose direct references and nuspec edges hold for every framework it builds."""

    framework: str
    nodes: dict[str, PackageNode] = field(default_factory=dict)  # case-folded id -> node
    direct: list[PackageRef] = field(default_factory=list)

    def node(self, package: str) -> PackageNode | None:
        return self.nodes.get(package.casefold())


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

    def targets_for(self, framework: str) -> list[Target]:
        """The targets a walk under `framework` enters: the target of that framework, else every one."""
        return [target for target in self.targets if same_framework(target.framework, framework)] or self.targets


@dataclass
class Graph:
    root: Path
    base: Path
    projects: dict[str, Project] = field(default_factory=dict)  # same_path -> project
    unreadable: list[tuple[Path, str]] = field(default_factory=list)
    central: dict[str, dict[str, str]] = field(default_factory=dict)  # same_path of a props file -> its versions

    def relative(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.base.resolve()).as_posix()
        except ValueError:
            return path.resolve().as_posix()

    def label(self, project: Project) -> str:
        return self.relative(project.file.path)

    def project_at(self, path: Path) -> Project | None:
        return self.projects.get(same_path(path))

    def refuse(self, error: Unreadable) -> None:
        self.unreadable.append((error.file, error.reason))

    def central_versions(self, project_dir: Path) -> dict[str, str]:
        """The central versions that apply in a project's directory, each props file read once; an unreadable one
        is reported once and gives none, so each project it covers keeps its own facts."""
        found = central_versions_file(project_dir, self.base)
        if found is None:
            return {}
        key = same_path(found)
        if key not in self.central:
            try:
                self.central[key] = read_central_versions(found)
            except Unreadable as error:
                self.refuse(error)
                self.central[key] = {}
        return self.central[key]


class Emitter:
    """Prints each fact line and remembers whether a finding was among them."""

    def __init__(self, emit: Callable[[str], None]) -> None:
        self.emit = emit
        self.findings = 0

    def line(self, key: str, *fields: object) -> None:
        if key in FINDING_KEYS:
            self.findings += 1
        self.emit("\t".join([key, *(flat_text(str(value)) for value in fields)]))


def raise_error(error: OSError) -> None:
    raise error


def find_projects(directory: Path) -> list[Path]:
    """Every .csproj under the directory, skipping build, dependency, and version-control directories."""
    found: list[Path] = []
    for current, directories, files in os.walk(directory, onerror=raise_error):
        directories[:] = sorted(name for name in directories if name.casefold() not in IGNORED_DIRECTORIES)
        found += [Path(current) / name for name in sorted(files) if name.casefold().endswith(PROJECT_SUFFIX)]
    return found


def resolve_root(path: Path, graph_unreadable: list[tuple[Path, str]]) -> tuple[Path, Path, list[Path]]:
    """The solution or directory the graph is built from, the directory paths are shown relative to, and the
    project files to read: a solution's listed .csproj files, the one solution in a directory, or the .csproj files
    under a directory holding none. A solution that cannot be read is reported unreadable and lists no project, so
    the answer says why it is empty."""
    path = path.resolve()
    if path.is_file():
        if not is_solution(path):
            raise Failed(f"{path} is not a .sln or .slnx solution")
        projects: list[Path] = []
        try:
            entries = read_listed_projects(path)
        except XmlRefused as error:
            graph_unreadable.append((path, str(error)))
            entries = []
        except OSError as error:
            graph_unreadable.append((path, error.strerror or str(error)))
            entries = []
        for listed in entries:
            if not listed.casefold().endswith(PROJECT_SUFFIX):
                continue
            candidate = (path.parent / listed.replace("\\", "/")).resolve()
            if candidate.is_file():
                projects.append(candidate)
            else:
                graph_unreadable.append((candidate, "listed by the solution but not found"))
        return path, path.parent, projects
    if not path.is_dir():
        raise Failed(f"{path} is neither a solution file nor a directory")
    solutions = sorted(child for child in path.iterdir() if child.is_file() and is_solution(child))
    if len(solutions) > 1:
        names = ", ".join(solution.name for solution in solutions)
        raise Failed(f"{path} holds more than one solution ({names}); name one with --root")
    if solutions:
        return resolve_root(solutions[0], graph_unreadable)
    projects = find_projects(path)
    if not projects:
        raise Failed(f"no solution or .csproj found under {path}")
    return path, path, projects


def load_projects(graph: Graph, listed: Iterable[Path]) -> None:
    """Read each listed project and, through ProjectReference, every project they reach."""
    queue = deque((path, True) for path in listed)
    while queue:
        path, is_listed = queue.popleft()
        if same_path(path) in graph.projects:
            continue
        try:
            file = read_project(path, graph.central_versions)
        except Unreadable as error:
            graph.refuse(error)
            placeholder = ProjectFile(path, "legacy", path.stem, [], [], [], [])
            graph.projects[same_path(path)] = Project(placeholder, is_listed, readable=False)
            continue
        graph.projects[same_path(path)] = Project(file, is_listed)
        queue.extend((reference, False) for reference in file.project_refs)


def read_configs(graph: Graph, project: Project) -> None:
    """The binding redirects of the project's web.config and app.config, when it has them."""
    for candidate in sorted(project.file.path.parent.iterdir()):
        if candidate.is_file() and candidate.name.casefold() in CONFIG_FILES:
            try:
                project.file.redirects += read_redirects(candidate)
            except Unreadable as error:
                graph.refuse(error)


def nuget_cache() -> list[Path]:
    """The folders the NuGet cache may be in: NUGET_PACKAGES, and ~/.nuget/packages."""
    folders: list[Path] = []
    configured = os.environ.get("NUGET_PACKAGES")
    if configured:
        folders.append(Path(configured))
    folders.append(Path.home() / ".nuget" / "packages")
    return [folder for folder in folders if folder.is_dir()]


def packages_folder(project_dir: Path, stop: Path) -> Path | None:
    """The nearest `packages` folder from the project's directory up to the solution's, or None."""
    directory = project_dir
    while True:
        candidate = directory / "packages"
        if candidate.is_dir():
            return candidate
        if same_path(directory) == same_path(stop) or directory.parent == directory:
            return None
        directory = directory.parent


def package_directory(folders: Sequence[Path], package: str, version: str) -> Path | None:
    """The folder a package at a version is unpacked in: `<id>.<version>` in a packages folder, or
    `<id>/<version>` in the NuGet cache, matched without regard to case."""
    for folder in folders:
        wanted = {f"{package}.{version}".casefold(), f"{package}/{version}".casefold()}
        try:
            for child in folder.iterdir():
                if child.name.casefold() == f"{package}.{version}".casefold() and child.is_dir():
                    return child
                if child.name.casefold() == package.casefold() and child.is_dir():
                    for inner in child.iterdir():
                        if f"{child.name}/{inner.name}".casefold() in wanted and inner.is_dir():
                            return inner
        except OSError:
            continue
    return None


def nuspec_node(graph: Graph, folders: Sequence[Path], reference: PackageRef) -> PackageNode | None:
    """The package's node from its nuspec in one of the folders, or None when it is not unpacked there."""
    if not reference.version:
        return None
    directory = package_directory(folders, reference.id, reference.version)
    if directory is None:
        return None
    nuspecs = [path for path in directory.iterdir() if path.name.casefold() == f"{reference.id}.nuspec".casefold()]
    if not nuspecs:
        return None
    try:
        return read_nuspec(nuspecs[0], package_assemblies(directory))
    except Unreadable as error:
        graph.refuse(error)
        return None


def closure_from_nuspecs(graph: Graph, project: Project, direct: list[PackageRef], folders: Sequence[Path]) -> None:
    """One target of the direct packages with their nodes from their nuspecs; edges are `nuspec` when any was
    found."""
    target = Target(NO_FRAMEWORK, direct=direct)
    for reference in direct:
        node = nuspec_node(graph, folders, reference)
        if node is not None:
            target.nodes[reference.id.casefold()] = node
    project.targets = [target]
    project.edges = "nuspec" if target.nodes else "direct"


def direct_references(graph: Graph, project: Project, assets: Assets, framework: str) -> list[PackageRef]:
    """A framework's direct packages: those its restore recorded, an empty list included, else the project file's
    references whose conditions hold for it, each item whose condition cannot be evaluated reported and left out."""
    if framework in assets.direct:
        return assets.direct[framework]
    references, unevaluated = packages_for(project.file.package_items, framework)
    for item, condition in unevaluated:
        reason = f"the condition {condition} on PackageReference {item.id} cannot be evaluated for {framework}"
        graph.refuse(Unreadable(project.file.path, reason))
    return references


def closure_from_assets(graph: Graph, project: Project, assets_path: Path, services: Services) -> None:
    """One target per framework the assets file restored, each with its own packages and direct references; the
    project keeps its direct target when the file cannot be read or restored no framework."""
    try:
        assets = read_assets(assets_path, project.file.frameworks)
    except Unreadable as error:
        graph.refuse(error)
        return
    if not assets.nodes:
        return
    project.targets = [
        Target(
            framework,
            {package.casefold(): node for package, node in nodes.items()},
            direct_references(graph, project, assets, framework),
        )
        for framework, nodes in assets.nodes.items()
    ]
    project.edges = "assets"
    fresh = assets_path.stat().st_mtime >= project.file.path.stat().st_mtime
    project.sdk_answers = fresh and services.which("dotnet") is not None


def build_closure(graph: Graph, project: Project, services: Services) -> None:
    """Fill the project's targets: the direct packages, their package nodes, and where their edges came from."""
    config = project.file.path.parent / PACKAGES_CONFIG
    if config.is_file():
        direct: list[PackageRef] = []
        try:
            direct = read_packages_config(config)
        except Unreadable as error:
            graph.refuse(error)
        folder = packages_folder(project.file.path.parent, graph.base)
        closure_from_nuspecs(graph, project, direct, [folder] if folder else nuget_cache())
        return
    project.targets = [Target(NO_FRAMEWORK, direct=list(project.file.packages))]
    assets_path = project.file.path.parent / "obj" / ASSETS_FILE
    if project.file.style == "sdk" and assets_path.is_file():
        closure_from_assets(graph, project, assets_path, services)


def build_graph(root: Path, services: Services) -> Graph:
    """Read the solution's projects and, for each one whose file could be read, its packages and redirects."""
    unreadable: list[tuple[Path, str]] = []
    resolved, base, listed = resolve_root(root, unreadable)
    graph = Graph(resolved, base, unreadable=unreadable)
    load_projects(graph, listed)
    for project in graph.projects.values():
        if project.readable:
            build_closure(graph, project, services)
            read_configs(graph, project)
    return graph


def print_graph(graph: Graph, out: Emitter) -> None:
    out.line("ROOT", graph.root.as_posix())
    for project in sorted(graph.projects.values(), key=lambda item: graph.label(item).casefold()):
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
    """Every chain from a project to a name under one framework, through project references, direct packages,
    package edges, and the assemblies a package ships. A referenced project is walked under the same framework when
    it has it, else under each of its own."""

    def __init__(self, graph: Graph, target: str, framework: str, limit: int | None = None) -> None:
        self.graph = graph
        self.target = target.casefold()
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
        key = same_path(project.file.path)
        if key in seen:
            return
        seen = seen | {key}
        for reference in project.file.project_refs:
            child = self.graph.project_at(reference)
            if child is not None:
                hop: Hop = ("project", child.file.assembly_name, "")
                if child.file.assembly_name.casefold() == self.target or child.file.path.stem.casefold() == self.target:
                    self.record((*path, hop))
                self.from_project(child, (*path, hop), seen)
        for target in project.targets_for(self.framework):
            for direct in target.direct:
                self.from_package(target, direct.id, direct.version, path, frozenset())
        for assembly in project.file.assemblies:
            if assembly.name.casefold() == self.target:
                self.record((*path, assembly_hop(assembly.name, assembly.version, assembly.hint_path)))

    def from_package(
        self, owner: Target, package: str, requested: str | None, path: tuple[Hop, ...], seen: frozenset[str]
    ) -> None:
        fold = package.casefold()
        if fold in seen:
            return
        path = (*path, package_hop(owner, package, requested))
        if fold == self.target:
            self.record(path)
            return
        node = owner.node(package)
        if node is None:
            return
        shipped = next((name for name in node.assemblies if name.casefold() == self.target), None)
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
    walked_projects = {same_path(project.file.path)}
    walked_packages: set[tuple[int, str]] = set()  # the target's identity and the package's case-folded id
    queue: deque[tuple[Step, str]] = deque()

    def own_steps(owner: Project) -> list[Step]:
        steps: list[Step] = []
        for reference in owner.file.project_refs:
            child = graph.project_at(reference)
            if child is not None:
                steps.append((("project", child.file.assembly_name, ""), None, child))
        for target in owner.targets_for(framework):
            steps += [(package_hop(target, direct.id, direct.version), target, None) for direct in target.direct]
        steps += [(assembly_hop(item.name, item.version, item.hint_path), None, None) for item in owner.file.assemblies]
        return steps

    def enqueue(steps: Iterable[Step], first: str | None) -> None:
        queue.extend((step, first or hop_text(step[0])) for step in steps)

    enqueue(own_steps(project), None)
    while queue:
        (hop, target, child), first = queue.popleft()
        kind, name, version = hop
        found.setdefault(
            (kind if kind != "framework" else "assembly", name.casefold(), version), Reached(kind, name, version, first)
        )
        if child is not None and same_path(child.file.path) not in walked_projects:
            walked_projects.add(same_path(child.file.path))
            enqueue(own_steps(child), first)
        elif target is not None and (id(target), name.casefold()) not in walked_packages:
            walked_packages.add((id(target), name.casefold()))
            node = target.node(name)
            if node is not None:
                enqueue(
                    [(package_hop(target, dep, wanted), target, None) for dep, wanted in node.dependencies.items()],
                    first,
                )
    return list(found.values())


def selected_projects(graph: Graph, narrowed: Path | None) -> list[Project]:
    """Every project in label order, or the one `--project` names."""
    if narrowed is None:
        return sorted(graph.projects.values(), key=lambda item: graph.label(item).casefold())
    project = graph.project_at(narrowed.resolve())
    if project is None:
        raise Failed(f"{narrowed} is not a project of {graph.root}")
    return [project]


def sdk_answers(
    graph: Graph, projects: list[Project], name: str, services: Services, out: Emitter
) -> dict[str, list[SdkPath]]:
    """The SDK's chains per project it answered, keyed by the project's path; each failed run is printed."""
    candidates = [project for project in projects if project.sdk_answers]
    if not candidates:
        return {}
    answer = run_why([project.file.path for project in candidates], name, services, DEFAULT_TIMEOUT_SECONDS)
    for project in candidates:
        failure = answer.failures.get(project.file.path)
        if failure:
            out.line("SDK_FAILED", graph.label(project), name, failure)
    return {
        same_path(project.file.path): answer.paths[project.file.path]
        for project in candidates
        if project.file.path in answer.answered
    }


def same_route(walked: tuple[Hop, ...], printed: SdkPath) -> bool:
    """Whether a chain of the graph walk takes the route the SDK printed: the same packages and projects in order,
    whatever their versions. The SDK prints each package at the version the answering project resolves, and the
    walk takes a referenced project's packages from that project's own restore, which may resolve another."""
    return len(walked) == len(printed.hops) and all(
        kind in ("package", "project") and name.casefold() == sdk_name.casefold()
        for (kind, name, _), (sdk_name, _) in zip(walked, printed.hops, strict=True)
    )


def merged_chain(printed: SdkPath, walked: list[tuple[Hop, ...]]) -> str:
    """The SDK's chain, with the hops of the walk's chains along the same route when there are any, so a project hop
    reads alike from both: each package at the SDK's version, and the walk's own versions of it named after `graph`
    where they differ."""
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
    assembly or one through a packages.config project, which the SDK cannot see, is named `graph`. Without the SDK's
    chains, the walk answers alone."""
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
        framework = next(
            (target.framework for target in project.targets if same_framework(target.framework, answer.framework)),
            answer.framework or NO_FRAMEWORK,
        )
        matched = [
            index
            for index, (walked_framework, chain) in enumerate(walked)
            if index not in claimed and same_framework(walked_framework, framework) and same_route(chain, answer)
        ]
        claimed.update(matched)
        rows.append((framework, ("sdk", merged_chain(answer, [walked[index][1] for index in matched]))))
    rows += [
        (framework, ("graph", " > ".join(hop_text(hop) for hop in chain)))
        for index, (framework, chain) in enumerate(walked)
        if index not in claimed
    ]
    printed = labelled(rows)
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
            project_paths(graph, project, name, answered.get(same_path(project.file.path)), out) for project in projects
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
        rows, key=lambda row: (row[1][2], row[1][0].casefold(), row[1][1])
    ):
        out.line("REACHES", graph.label(project), frameworks, name, version, kind, first_hop)
    out.line("SUMMARY", len(rows))


def resolved_assemblies(graph: Graph, project: Project, target: Target) -> list[tuple[str, str, str, str]]:
    """Each assembly the project's references resolve to under one target: name, kind, version, and what brings it
    in. A package's assemblies are those its restore or its unpacked folder shows, else the package id; the package
    closure is the target's own (a restore records what flows in through project references too), with the first
    hop that brings each package in where the graph walk finds one."""
    found: list[tuple[str, str, str, str]] = []
    for assembly in project.file.assemblies:
        kind = "reference" if assembly.hint_path else "framework"
        found.append((assembly.name, kind, assembly.version or "-", assembly.hint_path or "-"))
    first_hops = {
        (item.name.casefold(), item.version): item.first_hop
        for item in reaches(graph, project, target.framework)
        if item.kind == "package"
    }
    packages: dict[str, tuple[str, str]] = {
        node.id.casefold(): (node.id, node.version) for node in target.nodes.values()
    }
    for direct in target.direct:
        packages.setdefault(direct.id.casefold(), (direct.id, direct.version or ""))
    for fold, (package, version) in packages.items():
        node = target.node(package)
        names = node.assemblies if node and node.assemblies else (package,)
        via = first_hops.get((fold, version)) or hop_text(("package", package, version))
        found += [(name, "package", version or "-", via) for name in names]
    return found


def where_query(graph: Graph, assembly: str, narrowed: Path | None, out: Emitter) -> None:
    target_name = assembly.casefold()
    projects = 0
    for project in selected_projects(graph, narrowed):
        rows: list[tuple[str, Row]] = []
        for target in project.targets:
            rows += [
                (target.framework, (name, kind, version, via))
                for name, kind, version, via in resolved_assemblies(graph, project, target)
                if name.casefold() == target_name
            ]
            for reference in project.file.project_refs:
                child = graph.project_at(reference)
                if child is not None and child.file.assembly_name.casefold() == target_name:
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
            for assembly in project.file.assemblies
            if assembly.version
        ]
    return found


def at_odds(items: list[Want]) -> bool:
    """Whether two wants of one name differ in version where one build may load both: in two projects, or in one
    project under one framework. Two frameworks of one project are built apart."""
    return any(
        first.version != second.version
        and (first.project != second.project or same_framework(first.framework, second.framework))
        for first, second in combinations(items, 2)
    )


def redirect_lines(graph: Graph, project: Project, out: Emitter) -> int:
    """Check each binding redirect against the assembly versions the project references; return the stale count."""
    stale = 0
    referenced = {
        name.casefold() for target in project.targets for name, _, _, _ in resolved_assemblies(graph, project, target)
    }
    for redirect in project.file.redirects:
        fold = redirect.name.casefold()
        versions = sorted(
            {item.version for item in project.file.assemblies if item.name.casefold() == fold and item.version}
        )
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
            by_name.setdefault((want.kind, want.name.casefold()), []).append(want)
    names = 0
    for (kind, _), items in sorted(by_name.items(), key=lambda entry: (entry[0][1], entry[0][0])):
        if not at_odds(items):
            continue
        names += 1
        rows = labelled((item.framework, (item.name, item.version, item.project, item.via)) for item in items)
        for frameworks, (name, version, label, via) in sorted(rows, key=lambda row: (row[1][1], row[1][2].casefold())):
            out.line("CONFLICT", name, kind, version, label, frameworks, via)
    stale = sum(redirect_lines(graph, project, out) for project in projects)
    out.line("SUMMARY", names, stale)


Nodes = dict[str, tuple[str, str]]


def package_key(target: Target, package: str, requested: str | None) -> tuple[str, str]:
    """A package node's key and label, at the version the target resolved or asked for."""
    _, name, version = package_hop(target, package, requested)
    return f"package:{name.casefold()}@{version}", f"{name} {version}".strip()


def with_references(graph: Graph, projects: list[Project]) -> list[Project]:
    """The projects and every project they reach through project references, in label order."""
    found = {same_path(project.file.path): project for project in projects}
    queue = deque(projects)
    while queue:
        for reference in queue.popleft().file.project_refs:
            child = graph.project_at(reference)
            if child is not None and same_path(child.file.path) not in found:
                found[same_path(child.file.path)] = child
                queue.append(child)
    return sorted(found.values(), key=lambda item: graph.label(item).casefold())


def graph_edges(graph: Graph, projects: list[Project]) -> tuple[Nodes, list[tuple[str, str]]]:
    """Every node of the projects, keyed, with its label and kind, and every edge between keys, each once; a
    package's edges are drawn from every framework's target."""
    nodes: Nodes = {}
    edges: list[tuple[str, str]] = []

    def add(key: str, label: str, kind: str) -> str:
        nodes.setdefault(key, (label, kind))
        return key

    for project in projects:
        source = add(f"project:{same_path(project.file.path)}", graph.label(project), "project")
        for reference in project.file.project_refs:
            child = graph.project_at(reference)
            if child is not None:
                edges.append((source, add(f"project:{same_path(child.file.path)}", graph.label(child), "project")))
        for target in project.targets:
            for direct in target.direct:
                edges.append((source, add(*package_key(target, direct.id, direct.version), "package")))
            for node in target.nodes.values():
                owner = add(*package_key(target, node.id, node.version), "package")
                for dependency, wanted in node.dependencies.items():
                    edges.append((owner, add(*package_key(target, dependency, wanted), "package")))
        for assembly in project.file.assemblies:
            kind = "assembly" if assembly.hint_path else "framework"
            edges.append((source, add(f"{kind}:{assembly.name.casefold()}", assembly.name, kind)))
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


def output_path(requested: Path | None, fmt: str) -> Path:
    """Where the graph is written: the requested path, refused inside a skills directory, else a file in a new
    temporary directory named for the skill."""
    if requested is not None:
        owner = skills_directory_holding(requested)
        if owner is not None:
            raise Failed(f"{requested} is inside the skills directory {owner}; choose a path outside it")
        return requested
    return Path(tempfile.mkdtemp(prefix="dotnet-dependencies-")) / ("graph.dot" if fmt == "dot" else "graph.mmd")


def graph_query(graph: Graph, narrowed: Path | None, fmt: str, requested: Path | None, out: Emitter) -> None:
    target = output_path(requested, fmt)
    nodes, edges = graph_edges(graph, with_references(graph, selected_projects(graph, narrowed)))
    text = render_dot(nodes, edges) if fmt == "dot" else render_mermaid(nodes, edges)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")
    out.line("WROTE", target.resolve().as_posix(), len(nodes), len(edges))
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
    graph = build_graph(arguments.root, services)
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
        print(f"FAILED {error}")
        return 1
    return 1 if out.findings else 0


if __name__ == "__main__":
    run_main(main, errors="replace")
