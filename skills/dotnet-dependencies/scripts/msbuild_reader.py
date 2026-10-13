"""Readers for the files a .NET solution's dependency graph is built from, each into a plain record.

Every XML file goes through skill-core's `xml_reader`, which refuses a DOCTYPE or an entity before parsing, and the
project.assets.json a restore writes through the json module. A file a reader cannot read raises `Unreadable`, which
names the file and the reason, so the caller prints one UNREADABLE line and goes on: a bad file costs its own facts,
never the whole answer. Element names are matched without their namespace, because an old-style project carries the
MSBuild 2003 namespace, a nuspec one of several, and a binding redirect the assembly-binding one.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree.ElementTree import Element

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

from dotnet_solution import read_text
from xml_reader import XmlRefused, read_xml

ASSEMBLY_VERSION = re.compile(r"\bVersion\s*=\s*([^,\s]+)", re.IGNORECASE)
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


@dataclass
class ProjectFile:
    """What one .csproj says about its dependencies."""

    path: Path
    style: str  # sdk or legacy
    assembly_name: str
    frameworks: list[str]
    project_refs: list[Path]
    packages: list[PackageRef]
    assemblies: list[AssemblyRef]
    redirects: list[Redirect] = field(default_factory=list)


@dataclass(frozen=True)
class Assets:
    """What a restore recorded: each framework's resolved packages, and the direct dependencies."""

    nodes: dict[str, dict[str, PackageNode]]  # framework -> package id (as written) -> node
    direct: dict[str, list[PackageRef]]  # framework -> the packages the project asked for


def local_name(element: Element) -> str:
    """An element's tag without its namespace."""
    tag = element.tag
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def children(element: Element, name: str) -> list[Element]:
    """Every descendant named `name`, in document order, whatever its namespace."""
    return [child for child in element.iter() if local_name(child) == name]


def child_text(element: Element, name: str) -> str | None:
    """The stripped text of the first child element named `name`, or None."""
    for child in element:
        if local_name(child) == name:
            return (child.text or "").strip() or None
    return None


def parse_xml(path: Path) -> Element:
    """The root of the XML file at `path`, or `Unreadable` when it cannot be read or parsed."""
    try:
        text = read_text(path)
    except OSError as error:
        raise Unreadable(path, error.strerror or str(error)) from None
    try:
        return read_xml(text)
    except XmlRefused as error:
        raise Unreadable(path, str(error)) from None


def project_path(project_dir: Path, include: str) -> Path:
    """A path a project names, with Windows separators, resolved against the project's directory."""
    return (project_dir / include.replace("\\", "/")).resolve()


def is_sdk_style(root: Element) -> bool:
    """Whether a project is SDK-style: a Sdk attribute on the root, a Sdk element, or an Import of an Sdk."""
    if root.get("Sdk"):
        return True
    return any(local_name(child) == "Sdk" or child.get("Sdk") for child in root)


def frameworks_of(root: Element) -> list[str]:
    """The target frameworks a project names: TargetFramework, TargetFrameworks, or TargetFrameworkVersion."""
    found: list[str] = []
    for element in root.iter():
        name = local_name(element)
        text = (element.text or "").strip()
        if name in ("TargetFramework", "TargetFrameworkVersion") and text:
            found.append(text)
        elif name == "TargetFrameworks" and text:
            found += [part.strip() for part in text.split(";") if part.strip()]
    return list(dict.fromkeys(found))


def package_references(root: Element, central: dict[str, str]) -> list[PackageRef]:
    """Each PackageReference the project includes, with its Version attribute or child, else the central version,
    else none. Items are read in document order, as MSBuild evaluates them: an Include adds a reference, an Update
    with a version sets the version of each reference included before it under that id, and a Remove drops them; an
    Update or a Remove never adds one."""
    found: list[PackageRef] = []
    for element in children(root, "PackageReference"):
        version = element.get("Version") or child_text(element, "Version") or element.get("VersionOverride")
        if package := element.get("Include"):
            if version:
                found.append(PackageRef(package, version, "PackageReference"))
            elif package.casefold() in central:
                found.append(PackageRef(package, central[package.casefold()], "central"))
            else:
                found.append(PackageRef(package, None, "PackageReference"))
        elif (package := element.get("Update")) and version:
            found = [
                PackageRef(item.id, version, "PackageReference") if item.id.casefold() == package.casefold() else item
                for item in found
            ]
        elif package := element.get("Remove"):
            found = [item for item in found if item.id.casefold() != package.casefold()]
    return found


def assembly_references(root: Element) -> list[AssemblyRef]:
    """Each old-style Reference: the name before the first comma, the Version= in the Include, and the HintPath."""
    found: list[AssemblyRef] = []
    for element in children(root, "Reference"):
        include = (element.get("Include") or "").strip()
        if not include:
            continue
        name = include.split(",", 1)[0].strip()
        match = ASSEMBLY_VERSION.search(include)
        found.append(AssemblyRef(name, match.group(1) if match else None, child_text(element, "HintPath")))
    return found


def central_versions_file(project_dir: Path, stop: Path) -> Path | None:
    """The nearest Directory.Packages.props from the project's directory up to `stop`, or None."""
    directory = project_dir
    while True:
        candidate = directory / CENTRAL_VERSIONS
        if candidate.is_file():
            return candidate
        if directory == stop or directory.parent == directory:
            return None
        directory = directory.parent


def read_central_versions(path: Path) -> dict[str, str]:
    """The PackageVersion entries of a Directory.Packages.props, keyed by the case-folded package id; an unreadable
    file is Unreadable."""
    root = parse_xml(path)
    return {
        package.casefold(): version
        for element in children(root, "PackageVersion")
        if (package := element.get("Include")) and (version := element.get("Version"))
    }


def read_project(path: Path, central: Callable[[Path], dict[str, str]]) -> ProjectFile:
    """What the .csproj at `path` says about its dependencies. `central` gives an SDK-style project's central
    versions from the project's directory, so the caller reads each Directory.Packages.props once and reports an
    unreadable one once, without costing the project its own facts."""
    root = parse_xml(path)
    style = "sdk" if is_sdk_style(root) else "legacy"
    versions = central(path.parent) if style == "sdk" else {}
    return ProjectFile(
        path=path,
        style=style,
        assembly_name=child_text_anywhere(root, "AssemblyName") or path.stem,
        frameworks=frameworks_of(root),
        project_refs=[
            project_path(path.parent, include)
            for element in children(root, "ProjectReference")
            if (include := element.get("Include"))
        ],
        packages=package_references(root, versions),
        assemblies=assembly_references(root),
    )


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
        for redirect in dependent:
            if local_name(redirect) == "bindingRedirect":
                found.append(Redirect(path, name, redirect.get("oldVersion") or "", redirect.get("newVersion") or ""))
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


def read_assets(path: Path) -> Assets:
    """What a project.assets.json recorded: each framework's packages with their dependencies and assemblies, and
    the direct dependencies per framework. A runtime-specific target, such as `net8.0/win-x64`, repeats its
    framework's packages and is passed over."""
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise Unreadable(path, str(error)) from None
    targets = document.get("targets") if isinstance(document, dict) else None
    if not isinstance(targets, dict):
        raise Unreadable(path, "the assets file has no targets")
    nodes: dict[str, dict[str, PackageNode]] = {}
    for target, entries in targets.items():
        framework = target.split("/", 1)[0]
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
    frameworks = mapping(mapping(document, "project"), "frameworks")
    return Assets(nodes, assets_direct(frameworks))
