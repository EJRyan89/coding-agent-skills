"""A read-only dependency graph of a .NET solution: why a package or assembly is there, where an assembly is used,
which versions and binding redirects disagree, and the whole graph as a diagram.

    why <name> [<name> ...] [--root PATH] [--project PATH]   every path from each project to each name
    why --all --project PATH [--root PATH]                  everything one project reaches, with the first hop
    where <assembly> [--root PATH] [--project PATH]         the projects whose references resolve to the assembly
    conflicts [--root PATH] [--project PATH]                versions wanted at odds, and binding redirects to check
    graph [--format dot|mermaid] [--output FILE] [--root PATH]   the whole graph, written to a file, never printed

--root is a .sln or .slnx, a directory holding one, or a directory to search for *.csproj files; by default the
current directory. --project narrows a query to one project of the solution. Output is one tab-separated fact per
line, paths relative to the solution's directory with forward slashes:

    ROOT          <solution or directory>
    PROJECT       <project> <sdk|legacy> <assets|nuspec|direct> <listed|referenced>
                  where package-to-package edges came from: obj/project.assets.json, the packages' nuspecs in the
                  packages folder or the NuGet cache, or neither, leaving the direct references alone
    UNREADABLE    <file> <reason>                            a file the readers refused; its facts are missing
    PATH          <project> <name> <sdk|graph> <hop> > <hop> ...    one chain, each hop `name (vversion)`,
                  `name (project)`, or `name (vversion, assembly)`; the source says whether `dotnet nuget why`
                  answered or the graph walk did
    TRUNCATED     <project> <name> <limit>                   more chains than the limit; the first ones printed
    MISSING       <name>                                     no project reaches the name
    REACHES       <project> <name> <version> <package|project|assembly|framework> <first hop>
    WHERE         <project> <assembly> <reference|framework|package|project> <version> <via>
    CONFLICT      <name> <package|assembly> <version> <project> <via>   one line per project on each side
    REDIRECT_STALE <project> <config> <assembly> <newVersion> <versions referenced>
    REDIRECT_UNREFERENCED <project> <config> <assembly> <newVersion>
    WROTE         <file> <nodes> <edges>
    SUMMARY       the result: why prints <paths> <names found> <names missing>, or with --all <items reached>;
                  where <projects>; conflicts <names in conflict> <stale redirects>; graph <nodes> <edges>

`why` asks `dotnet nuget why` for a project that is SDK-style, uses PackageReference, has an obj/project.assets.json
no older than the project file, and only when dotnet is installed; every other project, and every project when the
SDK answers nothing, is walked from the graph. The script changes nothing on disk but the graph file, runs no restore
or build, and exits 1 when it printed a CONFLICT, REDIRECT_STALE, or UNREADABLE line, else 0; a FAILED <reason> line
and exit 1 when the root cannot be resolved, and a usage error exits 2.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from console import run_main
from dotnet_solution import is_solution, listed_projects, same_path
from flat_text import flat_text
from msbuild_reader import (
    ASSETS_FILE,
    CONFIG_FILES,
    PACKAGES_CONFIG,
    PackageNode,
    PackageRef,
    ProjectFile,
    Unreadable,
    package_assemblies,
    read_assets,
    read_nuspec,
    read_packages_config,
    read_project,
    read_redirects,
)
from sdk_why import DEFAULT_TIMEOUT_SECONDS, Services, run_why
from skill_roots import skills_directory_holding

PROJECT_SUFFIX = ".csproj"
IGNORED_DIRECTORIES = frozenset({".git", "bin", "obj", "node_modules", "packages", ".vs"})
PATH_LIMIT = 200
FINDING_KEYS = ("CONFLICT", "REDIRECT_STALE", "UNREADABLE")
Hop = tuple[str, str, str]  # kind (package, project, assembly, framework), name, version or ""


class Failed(Exception):
    """The query could not run; the message says why."""


@dataclass
class Project:
    """A project of the solution with the package closure its own files give."""

    file: ProjectFile
    listed: bool
    nodes: dict[str, PackageNode] = field(default_factory=dict)  # case-folded id -> node
    direct: list[PackageRef] = field(default_factory=list)
    edges: str = "direct"
    sdk_answers: bool = False

    def node(self, package: str) -> PackageNode | None:
        return self.nodes.get(package.casefold())


@dataclass
class Graph:
    root: Path
    base: Path
    projects: dict[str, Project] = field(default_factory=dict)  # same_path -> project
    unreadable: list[tuple[Path, str]] = field(default_factory=list)

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
    under a directory holding none."""
    path = path.resolve()
    if path.is_file():
        if not is_solution(path):
            raise Failed(f"{path} is not a .sln or .slnx solution")
        projects: list[Path] = []
        for listed in listed_projects(path):
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
            file = read_project(path, graph.base)
        except Unreadable as error:
            graph.refuse(error)
            graph.projects[same_path(path)] = Project(ProjectFile(path, "legacy", path.stem, [], [], [], []), is_listed)
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


def nuget_cache(package_folders: Sequence[Path] = ()) -> list[Path]:
    """The folders restored packages are kept in: the assets file's, NUGET_PACKAGES, and ~/.nuget/packages."""
    folders = [*package_folders]
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


def closure_from_nuspecs(graph: Graph, project: Project, folders: Sequence[Path]) -> None:
    """Nodes for the project's direct packages from their nuspecs; edges are `nuspec` when any was found."""
    for reference in project.direct:
        node = nuspec_node(graph, folders, reference)
        if node is not None:
            project.nodes[reference.id.casefold()] = node
    project.edges = "nuspec" if project.nodes else "direct"


def closure_from_assets(graph: Graph, project: Project, assets_path: Path, services: Services) -> bool:
    """The project's closure from its assets file; False when the file could not be read."""
    try:
        assets = read_assets(assets_path)
    except Unreadable as error:
        graph.refuse(error)
        return False
    framework = next((name for name in project.file.frameworks if name in assets.nodes), None)
    framework = framework or next(iter(assets.nodes), None)
    if framework is None:
        return False
    project.nodes = {package.casefold(): node for package, node in assets.nodes[framework].items()}
    project.direct = assets.direct.get(framework) or project.file.packages
    project.edges = "assets"
    fresh = assets_path.stat().st_mtime >= project.file.path.stat().st_mtime
    project.sdk_answers = fresh and services.which("dotnet") is not None
    return True


def build_closure(graph: Graph, project: Project, services: Services) -> None:
    """Fill the project's direct packages, its package nodes, and where their edges came from."""
    config = project.file.path.parent / PACKAGES_CONFIG
    if config.is_file():
        try:
            project.direct = read_packages_config(config)
        except Unreadable as error:
            graph.refuse(error)
        folder = packages_folder(project.file.path.parent, graph.base)
        closure_from_nuspecs(graph, project, [folder] if folder else nuget_cache())
        return
    project.direct = list(project.file.packages)
    assets_path = project.file.path.parent / "obj" / ASSETS_FILE
    if (
        project.file.style == "sdk"
        and assets_path.is_file()
        and closure_from_assets(graph, project, assets_path, services)
    ):
        return
    project.edges = "direct"


def build_graph(root: Path, services: Services) -> Graph:
    unreadable: list[tuple[Path, str]] = []
    resolved, base, listed = resolve_root(root, unreadable)
    graph = Graph(resolved, base, unreadable=unreadable)
    load_projects(graph, listed)
    for project in graph.projects.values():
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


def package_hop(project: Project, package: str, requested: str | None) -> Hop:
    """A package hop with the version the project resolved, else the version it asked for."""
    node = project.node(package)
    return ("package", node.id if node else package, node.version if node else requested or "")


def assembly_hop(name: str, version: str | None, hint_path: str | None) -> Hop:
    return ("assembly" if hint_path else "framework", name, version or "")


class PathFinder:
    """Every chain from a project to a name, through project references, direct packages, and package edges."""

    def __init__(self, graph: Graph, target: str, limit: int | None = None) -> None:
        self.graph = graph
        self.target = target.casefold()
        self.limit = PATH_LIMIT if limit is None else limit
        self.found: list[tuple[Hop, ...]] = []
        self.truncated = False

    def record(self, path: tuple[Hop, ...]) -> None:
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
        for direct in project.direct:
            self.from_package(project, direct.id, direct.version, path, frozenset())
        for assembly in project.file.assemblies:
            if assembly.name.casefold() == self.target:
                self.record((*path, assembly_hop(assembly.name, assembly.version, assembly.hint_path)))

    def from_package(
        self, project: Project, package: str, requested: str | None, path: tuple[Hop, ...], seen: frozenset[str]
    ) -> None:
        fold = package.casefold()
        if fold in seen:
            return
        hop = package_hop(project, package, requested)
        path = (*path, hop)
        if fold == self.target:
            self.record(path)
            return
        node = project.node(package)
        if node is None:
            return
        for dependency, version in node.dependencies.items():
            self.from_package(project, dependency, version, path, seen | {fold})


@dataclass(frozen=True)
class Reached:
    """Something a project reaches, with how it was first reached."""

    kind: str
    name: str
    version: str
    first_hop: str


def reaches(graph: Graph, project: Project) -> list[Reached]:
    """Everything the project reaches, breadth first, each name once per version, with the direct reference that
    brought it in first. A package's edges are its owner project's, so a package met again through another project
    is walked again there: the two projects may resolve it to different versions."""
    found: dict[tuple[str, str, str], Reached] = {}
    walked: set[tuple[str, str, str]] = set()
    queue: deque[tuple[Project, Hop, str]] = deque()

    def enqueue(owner: Project, hops: Iterable[Hop], first: str | None) -> None:
        queue.extend((owner, hop, first or hop_text(hop)) for hop in hops)

    def own_hops(owner: Project) -> list[Hop]:
        children = [graph.project_at(reference) for reference in owner.file.project_refs]
        projects: list[Hop] = [("project", child.file.assembly_name, "") for child in children if child is not None]
        packages = [package_hop(owner, direct.id, direct.version) for direct in owner.direct]
        assemblies = [assembly_hop(item.name, item.version, item.hint_path) for item in owner.file.assemblies]
        return [*projects, *packages, *assemblies]

    enqueue(project, own_hops(project), None)
    while queue:
        owner, hop, first = queue.popleft()
        kind, name, version = hop
        found.setdefault(
            (kind if kind != "framework" else "assembly", name.casefold(), version), Reached(kind, name, version, first)
        )
        if (same_path(owner.file.path), kind, name.casefold()) in walked:
            continue
        walked.add((same_path(owner.file.path), kind, name.casefold()))
        if kind == "project":
            child = next(
                (
                    item
                    for item in (graph.project_at(ref) for ref in owner.file.project_refs)
                    if item is not None and item.file.assembly_name == name
                ),
                None,
            )
            if child is not None and (same_path(child.file.path), "walked", "") not in walked:
                walked.add((same_path(child.file.path), "walked", ""))
                enqueue(child, own_hops(child), first)
        elif kind == "package" and (node := owner.node(name)) is not None:
            enqueue(owner, [package_hop(owner, dep, wanted) for dep, wanted in node.dependencies.items()], first)
    return list(found.values())


def selected_projects(graph: Graph, narrowed: Path | None) -> list[Project]:
    """Every project in label order, or the one `--project` names."""
    if narrowed is None:
        return sorted(graph.projects.values(), key=lambda item: graph.label(item).casefold())
    project = graph.project_at(narrowed.resolve())
    if project is None:
        raise Failed(f"{narrowed} is not a project of {graph.root}")
    return [project]


def sdk_paths(
    graph: Graph, projects: list[Project], name: str, services: Services, out: Emitter
) -> tuple[set[str], int]:
    """Print the SDK's chains for the projects it answers; return the labels it did not answer and the chain count."""
    candidates = [project for project in projects if project.sdk_answers]
    unanswered = {graph.label(project) for project in projects if not project.sdk_answers}
    if not candidates:
        return unanswered, 0
    answer = run_why([project.file.path for project in candidates], name, services, DEFAULT_TIMEOUT_SECONDS)
    count = 0
    for project in candidates:
        if project.file.path.stem not in answer.answered:
            unanswered.add(graph.label(project))
            continue
        for chain in answer.paths.get(project.file.path.stem, []):
            hops = " > ".join(hop_text(("package", hop, version)) for hop, version in chain.hops)
            out.line("PATH", graph.label(project), name, "sdk", hops)
            count += 1
    return unanswered, count


def graph_paths(graph: Graph, project: Project, name: str, out: Emitter) -> int:
    """Print the graph walk's chains from the project to the name; return how many."""
    finder = PathFinder(graph, name)
    finder.from_project(project, (), frozenset())
    for chain in finder.found:
        out.line("PATH", graph.label(project), name, "graph", " > ".join(hop_text(hop) for hop in chain))
    if finder.truncated:
        out.line("TRUNCATED", graph.label(project), name, finder.limit)
    return len(finder.found)


def why_query(graph: Graph, names: list[str], narrowed: Path | None, services: Services, out: Emitter) -> None:
    projects = selected_projects(graph, narrowed)
    paths, found, missing = 0, 0, 0
    for name in names:
        unanswered, count = sdk_paths(graph, projects, name, services, out)
        for project in projects:
            if graph.label(project) in unanswered:
                count += graph_paths(graph, project, name, out)
        if count:
            found += 1
        else:
            out.line("MISSING", name)
            missing += 1
        paths += count
    out.line("SUMMARY", paths, found, missing)


def why_all_query(graph: Graph, narrowed: Path, out: Emitter) -> None:
    (project,) = selected_projects(graph, narrowed)
    reached = reaches(graph, project)
    for item in sorted(reached, key=lambda item: (item.kind, item.name.casefold(), item.version)):
        out.line("REACHES", graph.label(project), item.name, item.version or "-", item.kind, item.first_hop)
    out.line("SUMMARY", len(reached))


def resolved_assemblies(graph: Graph, project: Project) -> list[tuple[str, str, str, str]]:
    """Each assembly the project's references resolve to: name, kind, version, and what brings it in. A package's
    assemblies are those its restore or its unpacked folder shows, else the package id; the package closure is the
    project's own (a restore records what flows in through project references too), with the first hop that
    brings each package in where the graph walk finds one."""
    found: list[tuple[str, str, str, str]] = []
    for assembly in project.file.assemblies:
        kind = "reference" if assembly.hint_path else "framework"
        found.append((assembly.name, kind, assembly.version or "-", assembly.hint_path or "-"))
    first_hops = {
        (item.name.casefold(), item.version): item.first_hop
        for item in reaches(graph, project)
        if item.kind == "package"
    }
    packages: dict[str, tuple[str, str]] = {
        node.id.casefold(): (node.id, node.version) for node in project.nodes.values()
    }
    for direct in project.direct:
        packages.setdefault(direct.id.casefold(), (direct.id, direct.version or ""))
    for fold, (package, version) in packages.items():
        node = project.node(package)
        names = node.assemblies if node and node.assemblies else (package,)
        via = first_hops.get((fold, version)) or hop_text(("package", package, version))
        found += [(name, "package", version or "-", via) for name in names]
    return found


def where_query(graph: Graph, assembly: str, narrowed: Path | None, out: Emitter) -> None:
    target = assembly.casefold()
    projects = 0
    for project in selected_projects(graph, narrowed):
        matched = False
        for name, kind, version, via in resolved_assemblies(graph, project):
            if name.casefold() == target:
                out.line("WHERE", graph.label(project), name, kind, version, via)
                matched = True
        for reference in project.file.project_refs:
            child = graph.project_at(reference)
            if child is not None and child.file.assembly_name.casefold() == target:
                out.line("WHERE", graph.label(project), child.file.assembly_name, "project", "-", graph.label(child))
                matched = True
        projects += matched
    out.line("SUMMARY", projects)


@dataclass(frozen=True)
class Want:
    name: str
    kind: str
    version: str
    project: str
    via: str


def wants(graph: Graph, project: Project) -> list[Want]:
    """Every package and assembly version the project wants, directly or through what it reaches."""
    label = graph.label(project)
    found = [
        Want(item.name, "package", item.version, label, item.first_hop)
        for item in reaches(graph, project)
        if item.kind == "package" and item.version
    ]
    found += [
        Want(assembly.name, "assembly", assembly.version, label, assembly.hint_path or "reference")
        for assembly in project.file.assemblies
        if assembly.version
    ]
    return found


def redirect_lines(graph: Graph, project: Project, out: Emitter) -> int:
    """Check each binding redirect against the assembly versions the project references; return the stale count."""
    stale = 0
    referenced = {name.casefold() for name, _, _, _ in resolved_assemblies(graph, project)}
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
        if len({item.version for item in items}) < 2:
            continue
        names += 1
        for item in sorted(items, key=lambda want: (want.version, want.project.casefold())):
            out.line("CONFLICT", item.name, kind, item.version, item.project, item.via)
    stale = sum(redirect_lines(graph, project, out) for project in projects)
    out.line("SUMMARY", names, stale)


Nodes = dict[str, tuple[str, str]]


def package_key(project: Project, package: str, requested: str | None) -> tuple[str, str]:
    """A package node's key and label, at the version the project resolved or asked for."""
    _, name, version = package_hop(project, package, requested)
    return f"package:{name.casefold()}@{version}", f"{name} {version}".strip()


def graph_edges(graph: Graph) -> tuple[Nodes, list[tuple[str, str]]]:
    """Every node, keyed, with its label and kind, and every edge between keys, each once."""
    nodes: Nodes = {}
    edges: list[tuple[str, str]] = []

    def add(key: str, label: str, kind: str) -> str:
        nodes.setdefault(key, (label, kind))
        return key

    for project in sorted(graph.projects.values(), key=lambda item: graph.label(item).casefold()):
        source = add(f"project:{same_path(project.file.path)}", graph.label(project), "project")
        for reference in project.file.project_refs:
            child = graph.project_at(reference)
            if child is not None:
                edges.append((source, add(f"project:{same_path(child.file.path)}", graph.label(child), "project")))
        for direct in project.direct:
            edges.append((source, add(*package_key(project, direct.id, direct.version), "package")))
        for node in project.nodes.values():
            owner = add(*package_key(project, node.id, node.version), "package")
            for dependency, wanted in node.dependencies.items():
                edges.append((owner, add(*package_key(project, dependency, wanted), "package")))
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


def graph_query(graph: Graph, fmt: str, requested: Path | None, out: Emitter) -> None:
    target = output_path(requested, fmt)
    nodes, edges = graph_edges(graph)
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
    diagram = commands.add_parser("graph", parents=[common], help="write the whole graph as a diagram")
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
        graph_query(graph, arguments.format, arguments.output, out)


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
