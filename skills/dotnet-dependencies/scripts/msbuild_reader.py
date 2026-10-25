"""Readers for the files a .NET solution's dependency graph is built from, each into a plain record.

Every XML file goes through ejryan89-skill-core's `xml_reader`, which refuses a DOCTYPE or an entity before parsing, and
the project.assets.json a restore writes through the json module. A file a reader cannot read raises `Unreadable`, which
names the file and the reason, so the caller prints one UNREADABLE line and goes on: a bad file costs its own facts,
never the whole answer. Element names are matched without their namespace (ejryan89-skill-core's `msbuild_xml`), because
an old-style project carries the MSBuild 2003 namespace, a nuspec one of several, and a binding redirect the
assembly-binding one.

A project file and the files it imports are evaluated as MSBuild evaluates them by `msbuild_evaluation`, which reads
them through `parse_xml`. Every comparison of two framework names goes through `same_framework`, so a restore's long
name, such as `.NETCoreApp,Version=v8.0`, matches the project's `net8.0`, and every search for a name among a project's
names through `matching_framework`, which prefers the name spelled alike.

The readers read only what the examined tree holds and only so much of it. A path a file names, a solution's entry, a
ProjectReference, or a project a restore recorded, is made absolute without reaching the file system
(ejryan89-skill-core's `project_path`), and the caller asks ejryan89-skill-core's `refusal` before anything opens or
looks it up: one outside the tree (`examined_tree`), or in a device or network form, is refused unread. A read opens
only a regular file and refuses one holding more than ejryan89-skill-core's `READ_LIMIT_BYTES` in `dotnet_solution.py`,
the one bound (`read_bounded`).
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree.ElementTree import Element

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

import dotnet_solution
import json_reader
from dotnet_solution import FileRefused, decode_text, lexical_absolute, listed_projects, project_path
from folders_up import folders_up
from msbuild_xml import local_name
from xml_reader import XmlRefused, read_xml

# What marks the folder a checkout starts in: a folder, or a file in a linked worktree or a submodule.
CHECKOUT_MARKER = ".git"

ASSEMBLY_VERSION = re.compile(r"\bVersion\s*=\s*([^,\s]+)", re.IGNORECASE)
# A target framework's long name, as older restores key their targets: `.NETCoreApp,Version=v8.0`.
LONG_FRAMEWORK = re.compile(
    r"^(?P<identifier>\.[a-z]+),Version=v(?P<version>\d+(?:\.\d+)*)(?:,Profile=(?P<profile>\w+))?$", re.IGNORECASE
)
# A short moniker with a platform, whose version a restore writes out where the project may leave it to the default.
PLATFORM_FRAMEWORK = re.compile(r"^(?P<base>net\d+\.\d+)-(?P<platform>[a-z]+)(?P<version>\d+(?:\.\d+)*)?$")
# The platform version the .NET SDK gives a platform a project names without one. A workload sets every other
# platform's, which varies with the workload, so a name without a version matches any version of such a platform.
PLATFORM_DEFAULT_VERSIONS = {"windows": "7.0"}
NUSPEC_DEPENDENCY_ID = "id"
PACKAGES_CONFIG = "packages.config"
CENTRAL_VERSIONS = "Directory.Packages.props"
ASSETS_FILE = "project.assets.json"
CONFIG_FILES = ("web.config", "app.config")


class Unreadable(Exception):
    """A file a reader could not read; `file` and `reason` are printed as one UNREADABLE line."""

    def __init__(self, file: Path, reason: str) -> None:
        super().__init__(f"{file}: {reason}")
        self.file = file
        self.reason = reason


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


def read_bounded(path: Path) -> bytes:
    """The bytes of the regular file at `path`, read by ejryan89-skill-core's `read_bounded`, or `Unreadable` with its
    reason when it refuses the file: it cannot be read, is not a regular file, or holds more than `READ_LIMIT_BYTES`."""
    try:
        return dotnet_solution.read_bounded(path)
    except FileRefused as error:
        raise Unreadable(path, error.reason) from None


def parse_xml(path: Path) -> Element:
    """The root of the XML file at `path`, or `Unreadable` when it cannot be read or parsed."""
    try:
        return read_xml(decode_text(read_bounded(path)))
    except XmlRefused as error:
        raise Unreadable(path, str(error)) from None


def read_solution(path: Path) -> list[str]:
    """The project paths the solution at `path` lists, as written, or `Unreadable` when it cannot be read or parsed."""
    try:
        return listed_projects(decode_text(read_bounded(path)), path)
    except XmlRefused as error:
        raise Unreadable(path, str(error)) from None


def examined_tree(base: Path) -> Path:
    """The tree a run reads the paths files name in: the checkout holding `base`, the solution's directory or the
    directory searched, which is the nearest folder at or above it holding `CHECKOUT_MARKER`, else `base` itself."""
    for folder in folders_up(base):
        try:
            if (folder / CHECKOUT_MARKER).exists():
                return folder
        except OSError:
            continue
    return base


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
        key = text.casefold()
        platform = PLATFORM_FRAMEWORK.match(key)
        if platform is None or not platform.group("version"):
            return key
        return f"{platform.group('base')}-{platform.group('platform')}{platform_version(platform.group('version'))}"
    identifier = long.group("identifier").casefold()
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
        return text.casefold()
    profile = long.group("profile")
    return f"{short}-{profile.casefold()}" if profile else short


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


def nearest_file(project_dir: Path, name: str) -> Path | None:
    """The nearest file named `name` from the project's directory up to the filesystem's root, as MSBuild searches
    for Directory.Build.props and Directory.Packages.props; None when there is none."""
    for directory in folders_up(project_dir):
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def child_text_anywhere(root: Element, name: str) -> str | None:
    """The stripped text of the first element named `name` anywhere under `root`, or None."""
    for element in children(root, name):
        text = (element.text or "").strip()
        if text:
            return text
    return None


def read_packages_config(path: Path) -> list[PackageRef]:
    """Each package entry of a packages.config with its version and target framework."""
    root = parse_xml(path)
    return [
        PackageRef(package, element.get("version"), PACKAGES_CONFIG, element.get("targetFramework"))
        for element in children(root, "package")
        if (package := element.get(NUSPEC_DEPENDENCY_ID))
    ]


def read_redirects(path: Path) -> list[Redirect]:
    """Each bindingRedirect of a web.config or app.config, with the assembly its dependentAssembly names."""
    root = parse_xml(path)
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


def read_nuspec(path: Path, assemblies: tuple[str, ...] = ()) -> PackageNode:
    """A package's id, version, and dependencies from its .nuspec, every dependency group together."""
    root = parse_xml(path)
    package = child_text_anywhere(root, NUSPEC_DEPENDENCY_ID)
    version = child_text_anywhere(root, "version")
    if not package or not version:
        raise Unreadable(path, "the nuspec names no package id and version")
    dependencies: dict[str, str] = {}
    for element in children(root, "dependency"):
        dependency = element.get(NUSPEC_DEPENDENCY_ID)
        if dependency and dependency.casefold() not in {key.casefold() for key in dependencies}:
            dependencies[dependency] = element.get("version") or ""
    return PackageNode(package, version, dependencies, assemblies)


def package_assemblies(package_dir: Path) -> tuple[str, ...]:
    """The stems of the assemblies a package folder ships under lib/, each once, in name order."""
    lib = package_dir / "lib"
    if not lib.is_dir():
        return ()
    return tuple(sorted({path.stem for path in lib.rglob("*.dll")}, key=str.casefold))


def assets_assemblies(entry: dict[str, Any]) -> tuple[str, ...]:
    """The assembly stems a restored package's compile entries name, each once."""
    compile_entries = entry.get("compile")
    if not isinstance(compile_entries, dict):
        return ()
    stems = {Path(key).stem for key in compile_entries if key.casefold().endswith(".dll")}
    return tuple(sorted(stems, key=str.casefold))


def mapping(document: dict[str, Any], key: str) -> dict[str, Any]:
    """The object under `key`, or an empty one when it is absent or not an object."""
    value = document.get(key)
    return value if isinstance(value, dict) else {}


def assets_direct(frameworks: dict[str, Any]) -> dict[str, list[PackageRef]]:
    """The packages the project asked for, per framework, from the assets file's project section."""
    direct: dict[str, list[PackageRef]] = {}
    for framework, document in frameworks.items():
        dependencies = document.get("dependencies") if isinstance(document, dict) else None
        direct[framework] = [
            PackageRef(package, detail.get("version") if isinstance(detail, dict) else None, "PackageReference")
            for package, detail in (dependencies.items() if isinstance(dependencies, dict) else [])
            if not isinstance(detail, dict) or detail.get("target", "Package") == "Package"
        ]
    return direct


def read_assets(path: Path, declared: Sequence[str] = ()) -> Assets:
    """What a project.assets.json recorded: each framework's packages with their dependencies and assemblies, and
    the direct dependencies per framework. A runtime-specific target, such as `net8.0/win-x64`, repeats its
    framework's packages and is passed over.

    Both are keyed by the name `$(TargetFramework)` holds for the framework, so its conditions read as written: the
    first of the project file's `declared` frameworks, then of the project section's, that is the same framework as
    the target, whichever form the restore wrote it in, a name spelled alike before one that leaves its platform
    version to the default (`matching_framework`), else the target's name as written."""
    try:
        document = json_reader.loads(read_bounded(path).decode("utf-8-sig"))
    except ValueError as error:
        raise Unreadable(path, str(error)) from None
    targets = document.get("targets") if isinstance(document, dict) else None
    if not isinstance(targets, dict):
        raise Unreadable(path, "the assets file has no targets")
    frameworks = mapping(mapping(document, "project"), "frameworks")
    aliases = [*declared, *frameworks]

    def named(written: str) -> str:
        return matching_framework(written, aliases) or written

    nodes: dict[str, dict[str, PackageNode]] = {}
    for target, entries in targets.items():
        framework = named(target.split("/", 1)[0])
        if not isinstance(entries, dict) or framework in nodes:
            continue
        nodes[framework] = {}
        for key, entry in entries.items():
            if not isinstance(entry, dict) or entry.get("type") != "package" or "/" not in key:
                continue
            package, version = key.split("/", 1)
            dependencies = entry.get("dependencies")
            nodes[framework][package] = PackageNode(
                package,
                version,
                {k: str(v) for k, v in dependencies.items()} if isinstance(dependencies, dict) else {},
                assets_assemblies(entry),
            )
    direct = {named(framework): refs for framework, refs in assets_direct(frameworks).items()}
    return Assets(nodes, direct, restored_project_refs(mapping(mapping(document, "project"), "restore"), named, path))


def restored_project_refs(restore: dict[str, Any], named: Callable[[str], str], path: Path) -> dict[str, list[Path]]:
    """The projects each framework references, from the assets file's `project.restore.frameworks.<framework>
    .projectReferences`, keyed as the frameworks are (`read_assets`); none when the restore does not name the project
    it ran for, against which its paths are rebased (`restored_path`)."""
    restored = restore.get("projectPath")
    if not isinstance(restored, str):
        return {}
    found: dict[str, list[Path]] = {}
    for framework, section in mapping(restore, "frameworks").items():
        references = section.get("projectReferences") if isinstance(section, dict) else None
        if isinstance(references, dict):
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
    while shared < min(len(base), len(parts) - 1) and parts[shared].casefold() == base[shared].casefold():
        shared += 1
    if shared == 0:
        return lexical_absolute("/".join(parts))
    return project_path(project_dir, "/".join([*[".."] * (len(base) - shared), *parts[shared:]]))
