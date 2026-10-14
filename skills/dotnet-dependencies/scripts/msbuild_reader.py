"""Readers for the files a .NET solution's dependency graph is built from, each into a plain record.

Every XML file goes through skill-core's `xml_reader`, which refuses a DOCTYPE or an entity before parsing, and the
project.assets.json a restore writes through the json module. A file a reader cannot read raises `Unreadable`, which
names the file and the reason, so the caller prints one UNREADABLE line and goes on: a bad file costs its own facts,
never the whole answer. Element names are matched without their namespace, because an old-style project carries the
MSBuild 2003 namespace, a nuspec one of several, and a binding redirect the assembly-binding one.

A PackageReference is kept with the conditions it sits under, its own and those of the ItemGroup, When, or Otherwise
holding it, and the Whens before it in its Choose, which must fail, so a framework's references can be evaluated for
that framework. Only comparisons of `$(TargetFramework)` and literals, and its StartsWith call, are evaluated; any
other condition is left unevaluated and named, never guessed. Every comparison of two framework names goes through
`same_framework`, so a restore's long name, such as `.NETCoreApp,Version=v8.0`, matches the project's `net8.0`, and
every search for a name among a project's names through `matching_framework`, which prefers the name spelled alike.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree.ElementTree import Element

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

from dotnet_solution import read_text
from xml_reader import XmlRefused, read_xml

ASSEMBLY_VERSION = re.compile(r"\bVersion\s*=\s*([^,\s]+)", re.IGNORECASE)
# One token of a condition: a quoted operand, the bare property, a StartsWith call on it, an operator, a parenthesis,
# or a keyword; anything else makes the condition one the reader does not evaluate.
CONDITION_TOKEN = re.compile(
    r"\s*(?:(?P<quoted>'[^']*')"
    r"|(?P<starts>\$\(TargetFramework\.StartsWith\('(?P<prefix>[^']*)'\)\))"
    r"|(?P<bare>\$\(TargetFramework\))"
    r"|(?P<operator>==|!=)"
    r"|(?P<paren>[()])"
    r"|(?P<keyword>and|or)\b)\s*",
    re.IGNORECASE,
)
FRAMEWORK_PROPERTY = re.compile(re.escape("$(TargetFramework)"), re.IGNORECASE)
# The property in either form a condition reads it: bare, or as the object of a StartsWith call.
FRAMEWORK_REFERENCE = re.compile(r"\$\(TargetFramework\b", re.IGNORECASE)
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


ConditionedElement = tuple[Element, tuple[tuple[str, bool], ...]]


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
class PackageItem:
    """A PackageReference as written: its operation (Include, Update, or Remove), the package, the version it states
    or the central version an Include takes, and the conditions it sits under, outermost first, each with whether it
    must hold (a When or an Otherwise in a Choose needs every When before it to fail)."""

    operation: str
    id: str
    version: str | None
    origin: str
    conditions: tuple[tuple[str, bool], ...] = ()


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
    package_items: list[PackageItem] = field(default_factory=list)


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


def platform_version(version: str) -> str:
    """A platform version as a restore writes it, its trailing zero parts dropped down to two parts: `10.0.19041.0`
    is `10.0.19041`, and `10` is `10.0`."""
    parts = version.split(".")
    while len(parts) > 2 and int(parts[-1]) == 0:
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
        modern = identifier == ".netcoreapp" and int(parts[0]) >= 5
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


class ConditionParser:
    """A recursive-descent reader of one MSBuild condition for one framework: `or` over `and` over comparisons, a
    StartsWith call on the property, and parentheses. Each method returns the value, or None when the condition is
    not in that form."""

    def __init__(self, tokens: list[re.Match[str]], framework: str) -> None:
        self.tokens = tokens
        self.framework = framework
        self.position = 0

    def peek(self, group: str) -> re.Match[str] | None:
        if self.position < len(self.tokens) and self.tokens[self.position].group(group) is not None:
            return self.tokens[self.position]
        return None

    def keyword(self, word: str) -> bool:
        token = self.peek("keyword")
        if token is not None and token.group("keyword").casefold() == word:
            self.position += 1
            return True
        return False

    def joined(self, word: str, operand: Callable[[], bool | None]) -> bool | None:
        values = [operand()]
        while self.keyword(word):
            values.append(operand())
        # One operand decides an `or` that holds or an `and` that fails, whatever the others are.
        deciding = word == "or"
        if deciding in values:
            return deciding
        return None if None in values else not deciding

    def disjunction(self) -> bool | None:
        return self.joined("or", self.conjunction)

    def conjunction(self) -> bool | None:
        return self.joined("and", self.factor)

    def factor(self) -> bool | None:
        if (token := self.peek("paren")) is not None and token.group("paren") == "(":
            self.position += 1
            value = self.disjunction()
            closing = self.peek("paren")
            if closing is None or closing.group("paren") != ")":
                return None
            self.position += 1
            return value
        if (token := self.peek("starts")) is not None:
            self.position += 1
            return self.framework.casefold().startswith(token.group("prefix").casefold())
        left = self.operand()
        operator = self.peek("operator")
        if left is None or operator is None:
            return None
        self.position += 1
        right = self.operand()
        if right is None:
            return None
        return (left.casefold() == right.casefold()) == (operator.group("operator") == "==")

    def operand(self) -> str | None:
        if (token := self.peek("bare")) is not None:
            self.position += 1
            return self.framework
        if (token := self.peek("quoted")) is not None:
            self.position += 1
            value = FRAMEWORK_PROPERTY.sub(lambda _: self.framework, token.group("quoted")[1:-1])
            return None if any(mark in value for mark in ("$(", "@(", "%(")) else value
        return None


def condition_holds(text: str, framework: str) -> bool | None:
    """Whether an MSBuild condition holds when `$(TargetFramework)` is `framework`, comparing as MSBuild does,
    without regard to case; None when the condition names another property or function, is malformed, or the
    framework is a restore's long name, such as `.NETFramework,Version=v4.7.2`, which no short name compares with."""
    tokens: list[re.Match[str]] = []
    position = 0
    while position < len(text):
        token = CONDITION_TOKEN.match(text, position)
        if token is None or token.end() == position:
            return None
        tokens.append(token)
        position = token.end()
    if not tokens or ("," in framework and FRAMEWORK_REFERENCE.search(text)):
        return None
    parser = ConditionParser(tokens, framework)
    value = parser.disjunction()
    return value if parser.position == len(tokens) else None


def conditioned(element: Element, conditions: tuple[tuple[str, bool], ...] = ()) -> Iterator[ConditionedElement]:
    """Every descendant in document order with the conditions it sits under, its own included. A Choose applies its
    first When whose condition holds, as MSBuild evaluates it, so a When also needs every When before it in its
    Choose to fail, and an Otherwise needs them all to; a When after one that cannot be evaluated is not evaluated
    either."""
    whens: list[str] = []
    for child in element:
        name = local_name(child)
        own = (child.get("Condition") or "").strip()
        inner = (*conditions, (own, True)) if own else conditions
        if name in ("When", "Otherwise"):
            inner = (*conditions, *((when, False) for when in whens))
            if name == "When" and own:
                inner = (*inner, (own, True))
                whens.append(own)
        yield child, inner
        yield from conditioned(child, inner)


def package_items(root: Element, central: dict[str, str]) -> list[PackageItem]:
    """Each PackageReference item in document order, one per package its Include, Update, or Remove lists, with its
    Version attribute or child, else for an Include the central version, and the conditions it sits under."""
    found: list[PackageItem] = []
    for element, conditions in conditioned(root):
        if local_name(element) != "PackageReference":
            continue
        version = element.get("Version") or child_text(element, "Version") or element.get("VersionOverride")
        operation = next((name for name in ("Include", "Update", "Remove") if element.get(name)), None)
        if operation is None:
            continue
        for package in item_specs(element.get(operation) or ""):
            if operation == "Include":
                origin, wanted = "PackageReference", version
                if not wanted and package.casefold() in central:
                    wanted, origin = central[package.casefold()], "central"
                found.append(PackageItem(operation, package, wanted, origin, conditions))
            else:
                wanted = version if operation == "Update" else None
                found.append(PackageItem(operation, package, wanted, "PackageReference", conditions))
    return found


def item_applies(item: PackageItem, framework: str | None) -> bool | str:
    """Whether the item applies under the framework, or the text of the condition that cannot be evaluated for it;
    with no framework every condition is taken to hold."""
    if framework is None:
        return True
    unevaluated: str | None = None
    for text, wanted in item.conditions:
        value = condition_holds(text, framework)
        if value is None:
            unevaluated = unevaluated or text
        elif value != wanted:
            return False
    return unevaluated or True


def packages_for(
    items: list[PackageItem], framework: str | None
) -> tuple[list[PackageRef], list[tuple[PackageItem, str]]]:
    """The references the items leave under a framework, read in document order as MSBuild evaluates them: an
    Include adds a reference, an Update with a version sets the version of each reference included before it under
    that id, and a Remove drops them; an Update or a Remove never adds one. An item whose condition holds is applied,
    one whose condition fails is not, and one whose condition cannot be evaluated is left out and returned with that
    condition. With no framework, as for a project with no restore, every item is applied whatever its condition."""
    found: list[PackageRef] = []
    unevaluated: list[tuple[PackageItem, str]] = []
    for item in items:
        applies = item_applies(item, framework)
        if isinstance(applies, str):
            unevaluated.append((item, applies))
            continue
        if not applies:
            continue
        fold = item.id.casefold()
        if item.operation == "Include":
            found.append(PackageRef(item.id, item.version, item.origin))
        elif item.operation == "Update" and item.version:
            found = [
                PackageRef(ref.id, item.version, "PackageReference") if ref.id.casefold() == fold else ref
                for ref in found
            ]
        elif item.operation == "Remove":
            found = [ref for ref in found if ref.id.casefold() != fold]
    return found, unevaluated


def assembly_references(root: Element) -> list[AssemblyRef]:
    """Each old-style Reference, one per assembly its Include lists: the name before the first comma, the Version=
    in the Include, and the HintPath."""
    found: list[AssemblyRef] = []
    for element in children(root, "Reference"):
        for include in item_specs(element.get("Include") or ""):
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
    items = package_items(root, versions)
    return ProjectFile(
        path=path,
        style=style,
        assembly_name=child_text_anywhere(root, "AssemblyName") or path.stem,
        frameworks=frameworks_of(root),
        project_refs=[
            project_path(path.parent, include)
            for element in children(root, "ProjectReference")
            for include in item_specs(element.get("Include") or "")
        ],
        packages=packages_for(items, None)[0],
        assemblies=assembly_references(root),
        package_items=items,
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


def read_assets(path: Path, declared: Sequence[str] = ()) -> Assets:
    """What a project.assets.json recorded: each framework's packages with their dependencies and assemblies, and
    the direct dependencies per framework. A runtime-specific target, such as `net8.0/win-x64`, repeats its
    framework's packages and is passed over.

    Both are keyed by the name `$(TargetFramework)` holds for the framework, so its conditions read as written: the
    first of the project file's `declared` frameworks, then of the project section's, that is the same framework as
    the target, whichever form the restore wrote it in, a name spelled alike before one that leaves its platform
    version to the default (`matching_framework`), else the target's name as written."""
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
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
    return Assets(nodes, {named(framework): refs for framework, refs in assets_direct(frameworks).items()})
