"""Readers for the files a .NET solution's dependency graph is built from, each into a plain record.

Every XML file goes through ejryan89-skill-core's `xml_reader`, which refuses a DOCTYPE or an entity before parsing, and
the project.assets.json a restore writes through its `json_reader`. A file a reader cannot read raises `Unreadable`,
which names the file and the reason, so the caller prints one UNREADABLE line and goes on: a bad file costs its own
facts, never the whole answer. Element names are matched without their namespace (ejryan89-skill-core's `msbuild_xml`),
because an old-style project carries the MSBuild 2003 namespace, a nuspec one of several, and a binding redirect the
assembly-binding one.

A project file and the files it imports are evaluated as MSBuild evaluates them by `msbuild_evaluation`, which reads
them through `parse_xml`. Every comparison of two framework names goes through `same_framework`, so a restore's long
name, such as `.NETCoreApp,Version=v8.0`, matches the project's `net8.0`, and every search for a name among a project's
names through `matching_framework`, which prefers the name spelled alike.

The readers read only what the examined tree holds and only so much of it. A path a file names, a solution's entry, a
ProjectReference, or a project a restore recorded, is made absolute without reaching the file system
(ejryan89-skill-core's `project_path`), and the caller asks ejryan89-skill-core's `refusal` before anything opens or
looks it up: one outside the tree (`examined_tree`), or in a device or network form, is refused unread. A file inside
the tree is stat or read only when ejryan89-skill-core's `folder_walk.outside_link_refusal` finds no link on its way,
its own name included, that leads outside the tree, judging each name from its own metadata and each link from its text,
so a link out of the tree is reported, never followed. Every read goes through ejryan89-skill-core's `TreeReader`, bound
to the examined tree, or to a NuGet cache folder for a package unpacked there, which judges each path so before it
touches it. A read opens only a regular file and refuses one holding more than ejryan89-skill-core's `READ_LIMIT_BYTES`
in `dotnet_solution.py`, the one bound (`read_bounded`). The two reads outside a reader are `examined_tree`, which finds
the tree before any is bound, and `exists_above_tree`, MSBuild's search for the nearest file going on above the tree,
which asks only whether a file is there and never reads it. Two limits: the
look at the links and the read are two steps, so a name replaced by a link between them is read through; and a hard
link is the file itself, with no link to judge, so a hard link to a file elsewhere is read as a file of the tree.
"""

from __future__ import annotations

import fnmatch
import re
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree.ElementTree import Element

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

import json_reader
from dotnet_solution import FileRefused, decode_text, lexical_absolute, listed_projects, project_path
from file_presence import is_directory, is_occupied
from folder_walk import LINKED_OUT_REASON, outside_link_refusal
from link_probe import is_link_or_junction
from msbuild_xml import local_name
from ordinal_case import ordinal_fold
from path_containment import within
from tree_reader import TreeReader, TreeRefused
from xml_reader import XmlRefused, read_xml

# What marks the folder a checkout starts in: a folder, or a file in a linked worktree or a submodule.
CHECKOUT_MARKER = ".git"

ASSEMBLY_VERSION = re.compile(r"\bVersion\s*=\s*([^,\s]+)", re.IGNORECASE | re.ASCII)
# A target framework's long name, as older restores key their targets: `.NETCoreApp,Version=v8.0`.
LONG_FRAMEWORK = re.compile(
    r"^(?P<identifier>\.[a-z]+),Version=v(?P<version>\d+(?:\.\d+)*)(?:,Profile=(?P<profile>\w+))?$",
    re.IGNORECASE | re.ASCII,
)
# A short moniker with a platform, whose version a restore writes out where the project may leave it to the default.
PLATFORM_FRAMEWORK = re.compile(r"^(?P<base>net\d+\.\d+)-(?P<platform>[a-z]+)(?P<version>\d+(?:\.\d+)*)?$", re.ASCII)
# The platform version the .NET SDK gives a platform a project names without one. A workload sets every other
# platform's, which varies with the workload, so a name without a version matches any version of such a platform.
PLATFORM_DEFAULT_VERSIONS = {"windows": "7.0"}
NUSPEC_DEPENDENCY_ID = "id"
PACKAGES_CONFIG = "packages.config"
CENTRAL_VERSIONS = "Directory.Packages.props"
ASSETS_FILE = "project.assets.json"
# What separates a restore's runtime-specific target from its framework, as in `net8.0/win-x64`.
RUNTIME_SEPARATOR = "/"
CONFIG_FILES = ("web.config", "app.config")


class Unreadable(Exception):
    """A file a reader could not read; `file` and `reason` are printed as one UNREADABLE line."""

    def __init__(self, file: Path, reason: str) -> None:
        super().__init__(f"{file}: {reason}")
        self.file = file
        self.reason = reason


class Refused(Unreadable):
    """A file a reader refused for where it lies, never for what it holds: outside the examined tree, or inside it
    with a link on its way that leads out of it (`outside_link_refusal`)."""


@dataclass(frozen=True)
class PackageRef:
    """A package a project asks for directly, with the version it asks for, which may be a range or absent."""

    id: str
    version: str | None
    origin: str  # PackageReference, central, or packages.config
    framework: str | None = None


@dataclass(frozen=True)
class AssemblyRef:
    """An old-style Reference: the assembly's name, the version its Include names, and its HintPath, if any."""

    name: str
    version: str | None
    hint_path: str | None


@dataclass(frozen=True)
class Redirect:
    """A bindingRedirect in a web.config or app.config."""

    config: Path
    name: str
    old_version: str
    new_version: str


@dataclass(frozen=True)
class PackageNode:
    """A package at one version with the packages it depends on and the assemblies it ships."""

    id: str
    version: str
    dependencies: dict[str, str]  # dependency id -> the version or range it asks for
    assemblies: tuple[str, ...] = ()


@dataclass(frozen=True)
class Assets:
    """What a restore recorded: each framework's resolved packages, the direct dependencies, and, where the restore
    recorded them, the projects each framework references."""

    nodes: dict[str, dict[str, PackageNode]]  # framework -> package id (as written) -> node
    direct: dict[str, list[PackageRef]]  # framework -> the packages the project asked for
    project_refs: dict[str, list[Path]] = field(default_factory=dict)  # framework -> the projects it references


def children(element: Element, name: str) -> list[Element]:
    """Every descendant named `name`, in document order, whatever its namespace."""
    return [child for child in element.iter() if local_name(child) == name]


def os_unreadable(error: OSError, fallback: Path) -> Unreadable:
    """An OSError as the `Unreadable` its caller reports against one file or package: the path the error names, else
    `fallback`, with the error's reason; a path a `TreeReader` refused is named with its refusal."""
    if isinstance(error, TreeRefused):
        return Unreadable(error.path, error.reason)
    return Unreadable(Path(error.filename) if error.filename else fallback, error.strerror or str(error))


def read_bounded(path: Path, reader: TreeReader) -> bytes:
    """The bytes of the regular file at `path`, read through `reader` within ejryan89-skill-core's `read_bounded`, or
    `Unreadable` with its reason when either refuses the file: the reader for where it lies or a link on its way leads,
    the bound because it cannot be read, is not a regular file, or holds more than `READ_LIMIT_BYTES`."""
    try:
        return reader.contents(path)
    except (FileRefused, TreeRefused) as error:
        raise Unreadable(path, error.reason) from None


def parse_xml(path: Path, reader: TreeReader) -> Element:
    """The root of the XML file at `path`, read through `reader`, or `Unreadable` when it cannot be read or parsed."""
    try:
        return read_xml(decode_text(read_bounded(path, reader)))
    except XmlRefused as error:
        raise Unreadable(path, str(error)) from None


def read_solution(path: Path, reader: TreeReader) -> list[str]:
    """The project paths the solution at `path` lists, as written, or `Unreadable` when it cannot be read or parsed."""
    try:
        return listed_projects(decode_text(read_bounded(path, reader)), path)
    except XmlRefused as error:
        raise Unreadable(path, str(error)) from None


def examined_tree(base: Path) -> Path:
    """The tree a run reads the paths files name in: the checkout holding `base`, the solution's directory or the
    directory searched, which is the innermost checkout holding it: the nearest folder at or above it holding
    `CHECKOUT_MARKER`, a folder or a file as a submodule's or a worktree's is, else `base` itself.

    The folders are looked at from the top down, as `base` names them, so a link on its way is met before anything is
    stat through it, and the look goes on below each checkout it meets, so a checkout inside another, such as a
    project's inside a dotfiles checkout at the home folder, a submodule, or a worktree, is the one chosen. A folder
    that is a link, a junction, or another reparse point is followed only while no checkout lies above it, as a link to
    the place a repository is kept, or macOS's /var, is: the user's own choice of where to look. One inside a checkout
    ends the look, and that checkout is the tree, so no marker is stat through a link that may lead out of it, whose
    refusal is the caller's (`outside_link_refusal`). A marker is judged from its own metadata and never followed, as
    the folder walk judges a nested checkout's, so a link there marks a checkout wherever it leads, even nowhere. A
    folder or a marker it cannot stat raises `Unreadable` naming it, which the command refuses, never read as no
    checkout there, which would take a folder above or the base for the tree; a folder that is not there ends the look,
    since nothing below it holds a marker."""
    absolute = base.absolute()
    found: Path | None = None
    for depth in range(1, len(absolute.parts) + 1):
        folder = Path(*absolute.parts[:depth])
        if depth > 1:
            try:
                linked = is_link_or_junction(folder)
                if linked and found is None:
                    # A link with no checkout above is followed, so one that leads nowhere it can be followed, such as
                    # a loop, is refused naming it rather than the marker below it.
                    folder.stat()
            except (FileNotFoundError, NotADirectoryError):
                break
            except OSError as error:
                raise os_unreadable(error, folder) from None
            if linked and found is not None:
                break
        marker = folder / CHECKOUT_MARKER
        try:
            if is_occupied(marker):
                found = folder
        except OSError as error:
            raise os_unreadable(error, marker) from None
    return base if found is None else found


# The reads made before a tree is bound, or above it, which ejryan89-skill-core's tree_reader does not judge.
TREE_READ_ALLOWED = {
    "examined_tree": (
        "finds the tree a run reads in, from the top down, before any tree is bound; it follows a link only while no "
        "checkout lies above it, the user's own choice of where to look, and judges each marker from its own metadata",
        ("dependency_graph:checkout_tree",),
    ),
    "exists_above_tree": (
        "MSBuild's search for the nearest file goes on above the examined tree, as the dotnet-dependencies row of "
        "docs/design.md states (the searches may go on above the tree on the local drive, as MSBuild's do, and a file "
        "one finds is read only inside the tree), so whether a file is there is asked of each folder above it, from "
        "the place's own metadata and its target's kind, and no file there is read",
        ("nearest_file",),
    ),
}


def platform_version(version: str) -> str:
    """A platform version as a restore writes it, its trailing zero parts dropped down to two parts: `10.0.19041.0`
    is `10.0.19041`, and `10` is `10.0`."""
    parts = version.split(".")
    while len(parts) > 2 and not parts[-1].strip("0"):
        parts.pop()
    return ".".join(parts if len(parts) > 1 else [*parts, "0"])


def framework_key(name: str) -> str:
    """A target framework's canonical name, its short moniker case-folded, whether it is written short, as `net8.0`
    or `net472`, or long, as `.NETCoreApp,Version=v8.0` or `.NETFramework,Version=v4.7.2`, the form older restores
    key their targets by, with a platform version as a restore writes it (`platform_version`). A name in neither
    form is its own key."""
    text = name.strip()
    long = LONG_FRAMEWORK.match(text)
    if long is None:
        key = ordinal_fold(text)
        platform = PLATFORM_FRAMEWORK.match(key)
        if platform is None or not platform.group("version"):
            return key
        return f"{platform.group('base')}-{platform.group('platform')}{platform_version(platform.group('version'))}"
    identifier = ordinal_fold(long.group("identifier"))
    parts = long.group("version").split(".")
    if identifier == ".netframework":
        while len(parts) > 2 and parts[-1] == "0":
            parts.pop()
        short = "net" + "".join(parts)
    elif identifier in (".netcoreapp", ".netstandard"):
        # Compared as text, so a version of any length is read: a major version of two digits or more is 5 or above.
        major = parts[0].lstrip("0") or "0"
        modern = identifier == ".netcoreapp" and (len(major) > 1 or major >= "5")
        short = ("net" if modern else identifier[1:]) + f"{parts[0]}.{parts[1] if len(parts) > 1 else '0'}"
    else:
        return ordinal_fold(text)
    profile = long.group("profile")
    return f"{short}-{ordinal_fold(profile)}" if profile else short


def same_framework(first: str, second: str) -> bool:
    """Whether two names are one target framework, the one comparison every framework match goes through: their
    canonical names agree, or both name one platform of one framework and one leaves its version to the default
    while the other names that default, as a restore keys `net8.0-windows7.0` for a project's `net8.0-windows` and
    never `net8.0-windows10.0.19041`. A platform whose default a workload sets (`PLATFORM_DEFAULT_VERSIONS`) takes
    any version as the default."""
    keys = (framework_key(first), framework_key(second))
    if keys[0] == keys[1]:
        return True
    left, right = (PLATFORM_FRAMEWORK.match(key) for key in keys)
    if not (
        left
        and right
        and (left.group("base"), left.group("platform")) == (right.group("base"), right.group("platform"))
    ):
        return False
    versions = [match.group("version") for match in (left, right) if match.group("version")]
    default = PLATFORM_DEFAULT_VERSIONS.get(left.group("platform"))
    return len(versions) == 1 and default in (None, versions[0])


def matching_framework(name: str, names: Iterable[str]) -> str | None:
    """The first of `names` that is the same framework as `name`, a name whose canonical name is `name`'s before one
    that is the same only by leaving its platform version to the default, so a project naming both `net8.0-android`
    and `net8.0-android34.0` keeps each of its restore's targets apart; None when none is."""
    candidates = list(names)
    key = framework_key(name)
    exact = next((candidate for candidate in candidates if framework_key(candidate) == key), None)
    return exact or next((candidate for candidate in candidates if same_framework(candidate, name)), None)


def item_specs(text: str) -> list[str]:
    """The item names an Include, Update, or Remove lists, separated by `;` as MSBuild splits them."""
    return [part.strip() for part in text.split(";") if part.strip()]


def msbuild_file_exists(path: Path, reader: TreeReader) -> bool:
    """MSBuild's file system's FileExists: whether anything but a folder, or a link to one, is at `path`, a link to
    nothing and a FIFO included; the stat's `OSError` when it cannot tell.

    MSBuild answers it with GetFileAttributes on Windows, which does not follow a link, and elsewhere with .NET's
    File.Exists, which reads the path with lstat, follows a link whose target is there, and is true for anything but a
    folder. A link to a folder that no longer exists, which Windows marks as a folder itself, is read here as a link to
    nothing, as File.Exists reads it elsewhere."""
    return reader.occupied(path) and not reader.folder(path, other_is_absent=True)


def exists_above_tree(path: Path) -> bool:
    """`msbuild_file_exists` of a candidate above the examined tree, where no reader is bound: whether anything but a
    folder is there, asked of the place's own metadata and its target's kind, never reading it."""
    return is_occupied(path) and not is_directory(path, other_is_absent=True)


def nearest_file(project_dir: Path, name: str, reader: TreeReader) -> Path | None:
    """The nearest file named `name` from the project's directory up to the filesystem's root, as MSBuild searches
    for Directory.Build.props and Directory.Packages.props: the first where `msbuild_file_exists` is true, so a link to
    nothing or a FIFO stops the search, as it stops MSBuild's, and its reader reports it as one it cannot read; None
    when there is none, and `Unreadable` naming a candidate whose stat fails, never read as none there, or, inside the
    reader's tree, `Refused` naming one that `outside_link_refusal` refuses, which is never stat. The folders are taken
    by text, a candidate inside the tree asked through `reader` and one above it by `exists_above_tree`."""
    directory = project_dir
    while True:
        candidate = directory / name
        inside = within(candidate, reader.tree)
        if inside and (linked := outside_link_refusal(candidate, reader.tree)) is not None:
            raise Refused(candidate, linked)
        try:
            if msbuild_file_exists(candidate, reader) if inside else exists_above_tree(candidate):
                return candidate
        except OSError as error:
            raise os_unreadable(error, candidate) from None
        if directory.parent == directory:
            return None
        directory = directory.parent


def child_text_anywhere(root: Element, name: str) -> str | None:
    """The stripped text of the first element named `name` anywhere under `root`, or None."""
    for element in children(root, name):
        text = (element.text or "").strip()
        if text:
            return text
    return None


def read_packages_config(path: Path, reader: TreeReader) -> list[PackageRef]:
    """Each package entry of a packages.config with its version and target framework."""
    root = parse_xml(path, reader)
    return [
        PackageRef(package, element.get("version"), PACKAGES_CONFIG, element.get("targetFramework"))
        for element in children(root, "package")
        if (package := element.get(NUSPEC_DEPENDENCY_ID))
    ]


def read_redirects(path: Path, reader: TreeReader) -> list[Redirect]:
    """Each bindingRedirect of a web.config or app.config, with the assembly its dependentAssembly names."""
    root = parse_xml(path, reader)
    found: list[Redirect] = []
    for dependent in children(root, "dependentAssembly"):
        identity = next((element for element in dependent if local_name(element) == "assemblyIdentity"), None)
        name = identity.get("name") if identity is not None else None
        if not name:
            continue
        found.extend(
            Redirect(path, name, redirect.get("oldVersion") or "", redirect.get("newVersion") or "")
            for redirect in dependent
            if local_name(redirect) == "bindingRedirect"
        )
    return found


def read_nuspec(path: Path, reader: TreeReader, assemblies: tuple[str, ...] = ()) -> PackageNode:
    """A package's id, version, and dependencies from its .nuspec, every dependency group together."""
    root = parse_xml(path, reader)
    package = child_text_anywhere(root, NUSPEC_DEPENDENCY_ID)
    version = child_text_anywhere(root, "version")
    if not package or not version:
        raise Unreadable(path, "the nuspec names no package id and version")
    dependencies: dict[str, str] = {}
    for element in children(root, "dependency"):
        dependency = element.get(NUSPEC_DEPENDENCY_ID)
        if dependency and ordinal_fold(dependency) not in {ordinal_fold(key) for key in dependencies}:
            dependencies[dependency] = element.get("version") or ""
    return PackageNode(package, version, dependencies, assemblies)


def package_assemblies(package_dir: Path, reader: TreeReader) -> tuple[str, ...]:
    """The stems of the assemblies a package folder ships under lib/ and every folder below it, each once, in name
    order, found by the reader's search, which judges each link entry from its own metadata and its text; or
    `Unreadable` when something other than a folder stands at lib/, whether one is there cannot be told, a folder below
    it cannot be listed or an entry examined, or a link at lib/ or below it, a symbolic link or a junction alike, leads
    outside the reader's tree, which is never listed or stat. A folder link that stays inside the tree is not entered,
    as the search enters none, a Windows junction among them, nor a folder holding a `.git` entry, which the search
    takes for a nested checkout, and the assemblies below either are not named."""
    lib = package_dir / "lib"
    if (refused := reader.refusal(lib)) is not None:
        raise Unreadable(lib, refused)
    try:
        if not reader.folder(lib):
            return ()
        walk = reader.search(lib, lambda name: fnmatch.fnmatch(name, "*.dll"), skipped=frozenset())
    except OSError as error:
        raise os_unreadable(error, lib) from None
    for entry in walk.not_entered:
        if entry.error is not None:
            raise os_unreadable(entry.error, entry.path)
        if entry.reason == LINKED_OUT_REASON:
            raise Unreadable(entry.path, entry.reason)
    for found in walk.files:
        if (refused := reader.refusal(found)) is not None:
            raise Unreadable(found, refused)
    return tuple(sorted({path.stem for path in walk.files}, key=ordinal_fold))


def assets_assemblies(entry: dict[str, Any], path: Path, where: str) -> tuple[str, ...]:
    """The assembly stems a restored package's compile entries name, each once; `where` names the package in the
    assets file at `path`, whose compile section of another kind than an object is Unreadable."""
    compile_entries = mapping(entry, "compile", path, f"{where}.compile")
    stems = {Path(key).stem for key in compile_entries if ordinal_fold(key).endswith(".dll")}
    return tuple(sorted(stems, key=ordinal_fold))


def mapping(document: dict[str, Any], key: str, path: Path, where: str) -> dict[str, Any]:
    """The object under `key`, `where` naming it in the assets file at `path`, or an empty one when it is absent;
    Unreadable when it holds another kind, whose content the reader cannot know."""
    value = document.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise Unreadable(path, f"the assets file's {where} is not an object")
    return value


def optional_text(value: object, path: Path, where: str) -> str | None:
    """`value` when it is text, None when it is absent, or Unreadable naming `where` in the assets file at `path`."""
    if value is not None and not isinstance(value, str):
        raise Unreadable(path, f"the assets file's {where} is not text")
    return value


def assets_direct(frameworks: dict[str, Any], path: Path) -> dict[str, list[PackageRef]]:
    """The packages the project asked for, per framework, from the assets file's project section at `path`; a
    framework's entry, its dependencies, or a dependency of another kind than an object, or a version or target
    that is not text, is Unreadable."""
    direct: dict[str, list[PackageRef]] = {}
    for framework, document in frameworks.items():
        where = f"project.frameworks.{framework}"
        if not isinstance(document, dict):
            raise Unreadable(path, f"the assets file's {where} is not an object")
        refs: list[PackageRef] = []
        for package, detail in mapping(document, "dependencies", path, f"{where}.dependencies").items():
            named = f"{where}.dependencies.{package}"
            if not isinstance(detail, dict):
                raise Unreadable(path, f"the assets file's {named} is not an object")
            target = optional_text(detail.get("target"), path, f"{named}.target")
            if (target or "Package") == "Package":
                version = optional_text(detail.get("version"), path, f"{named}.version")
                refs.append(PackageRef(package, version, "PackageReference"))
        direct[framework] = refs
    return direct


def runtime_target(framework: str, runtime: str) -> str:
    """The name of a restore's target: its framework's, with `/<runtime>` after it for a runtime-specific target."""
    return f"{framework}{RUNTIME_SEPARATOR}{runtime}" if runtime else framework


def target_framework(target: str) -> str:
    """The framework a restore's target builds, its runtime left off: what the project's build of it and its direct
    dependencies are keyed by."""
    return target.partition(RUNTIME_SEPARATOR)[0]


def read_assets(path: Path, reader: TreeReader, declared: Sequence[str] = ()) -> Assets:
    """What a project.assets.json recorded: each framework's packages with their dependencies and assemblies, and
    the direct dependencies per framework. A runtime-specific target, such as `net8.0/win-x64`, is a target of its own,
    keyed `<framework>/<runtime>` (`runtime_target`), since a package's runtime.json may add packages there that its
    framework's target lacks, as `dotnet nuget why` prints it; its direct dependencies are its framework's.

    Both are keyed by the name `$(TargetFramework)` holds for the framework, so its conditions read as written: the
    first of the project file's `declared` frameworks, then of the project section's, that is the same framework as
    the target, whichever form the restore wrote it in, a name spelled alike before one that leaves its platform
    version to the default (`matching_framework`), else the target's name as written."""
    try:
        document = json_reader.loads(read_bounded(path, reader).decode("utf-8-sig"))
    except ValueError as error:
        raise Unreadable(path, str(error)) from None
    targets = document.get("targets") if isinstance(document, dict) else None
    if not isinstance(targets, dict):
        raise Unreadable(path, "the assets file has no targets")
    project = mapping(document, "project", path, "project")
    frameworks = mapping(project, "frameworks", path, "project.frameworks")
    aliases = [*declared, *frameworks]

    def named(written: str) -> str:
        return matching_framework(written, aliases) or written

    nodes: dict[str, dict[str, PackageNode]] = {}
    for target, entries in targets.items():
        written, _, runtime = target.partition(RUNTIME_SEPARATOR)
        framework = runtime_target(named(written), runtime)
        if not isinstance(entries, dict):
            raise Unreadable(path, f"the assets file's target {target} is not an object")
        if framework in nodes:
            continue
        nodes[framework] = {}
        for key, entry in entries.items():
            where = f"target {target}'s {key}"
            if not isinstance(entry, dict):
                raise Unreadable(path, f"the assets file's {where} is not an object")
            if optional_text(entry.get("type"), path, f"{where}.type") != "package":
                continue
            if "/" not in key:
                raise Unreadable(path, f"the assets file's {where} is a package that names no version")
            package, version = key.split("/", 1)
            dependencies = mapping(entry, "dependencies", path, f"{where}.dependencies")
            for dependency, wanted in dependencies.items():
                if not isinstance(wanted, str):
                    raise Unreadable(path, f"the assets file's {where}.dependencies.{dependency} is not text")
            nodes[framework][package] = PackageNode(
                package, version, dict(dependencies), assets_assemblies(entry, path, where)
            )
    direct = {named(framework): refs for framework, refs in assets_direct(frameworks, path).items()}
    restore = mapping(project, "restore", path, "project.restore")
    return Assets(nodes, direct, restored_project_refs(restore, named, path))


def restored_project_refs(restore: dict[str, Any], named: Callable[[str], str], path: Path) -> dict[str, list[Path]]:
    """The projects each framework references, from the assets file's `project.restore.frameworks.<framework>
    .projectReferences`, keyed as the frameworks are (`read_assets`); none when the restore does not name the project
    it ran for, against which its paths are rebased (`restored_path`)."""
    restored = optional_text(restore.get("projectPath"), path, "project.restore.projectPath")
    if restored is None:
        return {}
    found: dict[str, list[Path]] = {}
    for framework, section in mapping(restore, "frameworks", path, "project.restore.frameworks").items():
        where = f"project.restore.frameworks.{framework}"
        if not isinstance(section, dict):
            raise Unreadable(path, f"the assets file's {where} is not an object")
        references = mapping(section, "projectReferences", path, f"{where}.projectReferences")
        # A section without projectReferences says nothing about them; an empty one says there are none.
        if section.get("projectReferences") is not None:
            # The project's directory holds the obj folder the assets file is in.
            found[named(framework)] = [restored_path(key, restored, path.parent.parent) for key in references]
    return found


def restored_path(recorded: str, restored_project: str, project_dir: Path) -> Path:
    """A project path a restore recorded, taken relative to the directory of the project the restore ran for and
    resolved from `project_dir`, so a checkout moved since its restore still names its own projects; a path sharing
    no root with that project is taken as written. Either is made absolute without reaching the file system
    (`project_path`), since it is only compared with the paths the project file names, never opened."""
    parts = recorded.replace("\\", "/").split("/")
    base = restored_project.replace("\\", "/").split("/")[:-1]
    shared = 0
    while shared < min(len(base), len(parts) - 1) and ordinal_fold(parts[shared]) == ordinal_fold(base[shared]):
        shared += 1
    if shared == 0:
        return lexical_absolute("/".join(parts))
    return project_path(project_dir, "/".join([*[".."] * (len(base) - shared), *parts[shared:]]))
