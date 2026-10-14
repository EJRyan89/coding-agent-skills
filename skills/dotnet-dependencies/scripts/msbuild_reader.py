"""Readers for the files a .NET solution's dependency graph is built from, each into a plain record.

Every XML file goes through skill-core's `xml_reader`, which refuses a DOCTYPE or an entity before parsing, and the
project.assets.json a restore writes through the json module. A file a reader cannot read raises `Unreadable`, which
names the file and the reason, so the caller prints one UNREADABLE line and goes on: a bad file costs its own facts,
never the whole answer. Element names are matched without their namespace, because an old-style project carries the
MSBuild 2003 namespace, a nuspec one of several, and a binding redirect the assembly-binding one.

Each PackageReference, Reference, ProjectReference, and central PackageVersion item is kept with the conditions it sits
under, its own and those of the ItemGroup, When, or Otherwise holding it, and the Whens before it in its Choose, which
must fail, so a framework's references can be evaluated for that framework: an item's own conditions with the value the
build of that framework gives `$(TargetFramework)` in the end, and a When's with the value it held when MSBuild entered
the Choose, which chooses its branch then, once (`ChooseEntry`). The target framework properties are read as the file
sets them, each condition evaluated once, when it is reached (`FrameworkProperties`). An item inside a Target is defined
only when that target runs, so evaluation leaves it out and the reader names it instead. Only comparisons of
`$(TargetFramework)` and literals, and its StartsWith call, are evaluated; any other condition is left unevaluated and
named, never guessed, and a project whose When has no Condition, which MSBuild refuses, is unreadable. Every comparison
of two framework names goes through `same_framework`, so a restore's long name, such as `.NETCoreApp,Version=v8.0`,
matches the project's `net8.0`, and every search for a name among a project's names through `matching_framework`, which
prefers the name spelled alike.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, TypeVar
from xml.etree.ElementTree import Element

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

from dotnet_solution import read_text, same_path
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
OPERATIONS = ("Include", "Update", "Remove")
# The properties that name a project's target frameworks; TargetFrameworks lists several, separated by `;`.
FRAMEWORK_PROPERTIES = ("TargetFrameworks", "TargetFramework", "TargetFrameworkVersion")
# The items the graph reads from a project file, which a Target holding them leaves out of evaluation.
PROJECT_ITEMS = ("PackageReference", "ProjectReference", "Reference")
CENTRAL_ITEM = "PackageVersion"


# A condition an item sits under, with whether it must hold and, for a When's, its Choose's `ChooseEntry`.
Conditions = tuple[tuple[str, bool, "ChooseEntry | None"], ...]
ConditionedElement = tuple[Element, Conditions]


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
    """A PackageReference, or with the origin `central` a PackageVersion, as written: its operation (Include, Update,
    or Remove), the package, the version it states, and the conditions it sits under, outermost first, each with
    whether it must hold (a When or an Otherwise in a Choose needs every When before it to fail). An Include that
    states no version takes the central version its framework gives it (`packages_for`)."""

    operation: str
    id: str
    version: str | None
    origin: str
    conditions: Conditions = ()


@dataclass(frozen=True)
class ReferenceItem:
    """An old-style Reference as written: its operation, the item its Include, Update, or Remove names, which may
    carry the assembly's version, the HintPath it states, and the conditions it sits under."""

    operation: str
    spec: str
    hint_path: str | None
    conditions: Conditions = ()


@dataclass(frozen=True)
class ProjectItem:
    """A ProjectReference as written: its operation, the project it names, resolved, and the conditions it sits
    under."""

    operation: str
    path: Path
    conditions: Conditions = ()


@dataclass(frozen=True)
class TargetItem:
    """An item inside a Target, which MSBuild defines only when that target runs, so evaluation leaves it out."""

    kind: str
    name: str
    target: str


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
    """What one .csproj says about its dependencies: its references as for a project with no restore, every item
    applied whatever its condition but a Remove that does not hold for every framework it names (`evaluated`), which
    are also the projects loaded as its references, and the items they come from, to evaluate for each framework."""

    path: Path
    style: str  # sdk or legacy
    assembly_name: str
    frameworks: list[str]
    project_refs: list[Path]
    packages: list[PackageRef]
    assemblies: list[AssemblyRef]
    redirects: list[Redirect] = field(default_factory=list)
    package_items: list[PackageItem] = field(default_factory=list)
    central_items: list[PackageItem] = field(default_factory=list)
    reference_items: list[ReferenceItem] = field(default_factory=list)
    project_items: list[ProjectItem] = field(default_factory=list)
    in_targets: list[TargetItem] = field(default_factory=list)


@dataclass(frozen=True)
class CentralVersions:
    """A Directory.Packages.props: its PackageVersion items, and those inside a Target, which evaluation leaves out."""

    items: list[PackageItem]
    in_targets: list[TargetItem] = field(default_factory=list)


@dataclass(frozen=True)
class Assets:
    """What a restore recorded: each framework's resolved packages, the direct dependencies, and, where the restore
    recorded them, the projects each framework references."""

    nodes: dict[str, dict[str, PackageNode]]  # framework -> package id (as written) -> node
    direct: dict[str, list[PackageRef]]  # framework -> the packages the project asked for
    project_refs: dict[str, list[Path]] = field(default_factory=dict)  # framework -> the projects it references


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
    """The target frameworks a project names in its TargetFrameworks, TargetFramework, and TargetFrameworkVersion
    properties outside a target, read in document order as MSBuild evaluates them (`FrameworkProperties`)."""
    return FrameworkProperties(root).frameworks()


# The values of a project's framework properties at one point of its evaluation, each a list of the names it may hold:
# a TargetFrameworks value its names, and a TargetFramework value one name, or several when the file may set any of
# them there, `""` among them where it may still be unset.
PropertyState = dict[str, list[str]]


def joined(states: Sequence[PropertyState]) -> PropertyState:
    """The state after one of several alternatives, not knowing which: each property may hold any value one of them
    leaves it, `""` where one leaves it unset."""
    names = dict.fromkeys(name for state in states for name in state)
    return {name: list(dict.fromkeys(value for state in states for value in state.get(name) or [""])) for name in names}


@dataclass(frozen=True)
class ChooseEntry:
    """The value `$(TargetFramework)` holds when MSBuild enters a Choose, which tests its Whens then and only then,
    whatever the file sets after, in the build of each framework: the framework itself where the file sets nothing to
    the property from the Choose on, or where TargetFrameworks names it, whose build fixes the property from the
    start; else the value the file has given it so far, `""` while unset, which the reader cannot evaluate a condition
    against while it is one of several."""

    values: tuple[str, ...]
    reassigned: bool
    built_apart: tuple[str, ...]

    def framework_at(self, framework: str) -> str | None:
        if not self.reassigned or matching_framework(framework, self.built_apart) is not None:
            return framework
        return self.values[0] if len(self.values) == 1 else None


class FrameworkProperties:
    """A project's target framework properties, read in document order as MSBuild evaluates them: each condition is
    evaluated once, when it is reached, a PropertyGroup's before its properties and a Choose's Whens before any branch
    applies, the first that holds, else the Otherwise, so a property a group or a branch sets never changes whether it
    applies. A property whose conditions hold sets its value, one whose conditions fail is passed over, and one whose
    condition cannot be evaluated adds its names to those it may replace, never choosing between them; a Choose whose
    branch cannot be chosen reads each branch that may apply from the state it was entered with, and the next element
    sees any value one of them, or none where no branch may apply, leaves. A condition reads `$(TargetFramework)` as
    the value the file has given it so far, empty at first; while that value is one of several, a condition reading it
    cannot be evaluated. The value each Choose is entered with is kept for its items (`choose_entries`)."""

    def __init__(self, root: Element) -> None:
        self.values: PropertyState = {}
        self.assignments = 0
        self.entered: dict[Element, tuple[tuple[str, ...], int]] = {}
        self.read(root)

    def frameworks(self) -> list[str]:
        return list(dict.fromkeys(framework for names in self.values.values() for framework in names if framework))

    def choose_entries(self) -> dict[Element, ChooseEntry]:
        """Each Choose the file enters, with the value `$(TargetFramework)` holds there for each framework's build."""
        built_apart = tuple(name for name in self.values.get("TargetFrameworks", []) if name)
        return {
            choose: ChooseEntry(values, self.assignments > assignments, built_apart)
            for choose, (values, assignments) in self.entered.items()
        }

    def holds(self, text: str) -> bool | None:
        """Whether a condition holds now, None when it cannot be evaluated."""
        current = self.values.get("TargetFramework") or [""]
        if len(current) > 1 and FRAMEWORK_REFERENCE.search(text):
            return None
        return condition_holds(text, current[0])

    def condition(self, element: Element) -> bool | None:
        """Whether a PropertyGroup's or a property's condition holds now; one with none does."""
        text = (element.get("Condition") or "").strip()
        return self.holds(text) if text else True

    def read(self, container: Element) -> None:
        """Read the PropertyGroups and Chooses of a project, When, or Otherwise."""
        for child in container:
            name = local_name(child)
            if name == "PropertyGroup" and (applies := self.condition(child)) is not False:
                for element in child:
                    self.set_property(element, applies is True)
            elif name == "Choose":
                self.read_choose(child)

    def read_choose(self, choose: Element) -> None:
        """Read the branch of a Choose that applies, or, when the reader cannot choose it, each branch that may, from
        the state the Choose was entered with, and join the states they leave with the one it was entered with when no
        branch may apply."""
        self.entered[choose] = (tuple(self.values.get("TargetFramework") or [""]), self.assignments)
        branches, chosen = self.branches(choose)
        if chosen and len(branches) == 1:
            self.read(branches[0])
            return
        incoming = self.values
        states = [] if chosen else [incoming]
        for branch in branches:
            self.values = {name: list(names) for name, names in incoming.items()}
            self.read(branch)
            states.append(self.values)
        self.values = joined(states)

    def branches(self, choose: Element) -> tuple[list[Element], bool]:
        """The branches of a Choose that may apply, every When tested before any branch is read: up to the first When
        that holds, else the Otherwise; and whether one of them surely applies, as one does once a When holds or the
        Otherwise is reached. A When with no Condition, which MSBuild refuses, cannot be evaluated."""
        found: list[Element] = []
        for branch in choose:
            name = local_name(branch)
            text = (branch.get("Condition") or "").strip()
            value = (self.holds(text) if text else None) if name == "When" else True
            if name not in ("When", "Otherwise") or value is False:
                continue
            found.append(branch)
            if value is True:
                return found, True
        return found, False

    def set_property(self, element: Element, certain: bool) -> None:
        """Apply a framework property whose condition does not fail: its value replaces the one before it when its
        group's condition and its own surely hold, and joins it otherwise."""
        name = local_name(element)
        applies = self.condition(element)
        if name not in FRAMEWORK_PROPERTIES or applies is False:
            return
        if name == "TargetFramework":
            self.assignments += 1
        text = (element.text or "").strip()
        if name == "TargetFrameworks":
            names = [part.strip() for part in text.split(";") if part.strip()]
        else:
            names = [text] if text else []
        self.values[name] = names if certain and applies is True else joined([self.values, {name: names}])[name]


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


class Malformed(Exception):
    """A condition that is not a well-formed expression of the forms the reader knows, such as one with a dangling
    `or` or an unclosed parenthesis."""


class ConditionParser:
    """A recursive-descent reader of one MSBuild condition for one framework: `or` over `and` over comparisons, a
    StartsWith call on the property, and parentheses. The whole condition is parsed whatever its operands' values, so
    an operand the reader cannot evaluate never stops it early. Each method returns the value, or None when it cannot
    be evaluated, and raises `Malformed` when the condition is not well formed."""

    def __init__(self, tokens: list[re.Match[str]], framework: str) -> None:
        self.tokens = tokens
        self.framework = framework
        self.position = 0

    def peek(self, group: str) -> re.Match[str] | None:
        if self.position < len(self.tokens) and self.tokens[self.position].group(group) is not None:
            return self.tokens[self.position]
        return None

    def take(self, group: str, text: str | None = None) -> re.Match[str] | None:
        """The next token when it is of `group`, and spelled `text` when given, past it; else None."""
        token = self.peek(group)
        if token is None or (text is not None and token.group(group).casefold() != text):
            return None
        self.position += 1
        return token

    def keyword(self, word: str) -> bool:
        return self.take("keyword", word) is not None

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
        if self.take("paren", "(") is not None:
            value = self.disjunction()
            if self.take("paren", ")") is None:
                raise Malformed
            return value
        if (token := self.take("starts")) is not None:
            # .NET's String.StartsWith(string), which MSBuild calls, compares with regard to case.
            return self.framework.startswith(token.group("prefix"))
        left = self.operand()
        operator = self.take("operator")
        if operator is None:
            # A lone operand is a string MSBuild reads as a boolean, which the reader does not evaluate.
            return None
        right = self.operand()
        if left is None or right is None:
            return None
        return (left.casefold() == right.casefold()) == (operator.group("operator") == "==")

    def operand(self) -> str | None:
        if self.take("bare") is not None:
            return self.framework
        if (token := self.take("quoted")) is not None:
            value = FRAMEWORK_PROPERTY.sub(lambda _: self.framework, token.group("quoted")[1:-1])
            return None if any(mark in value for mark in ("$(", "@(", "%(")) else value
        raise Malformed


def condition_holds(text: str, framework: str) -> bool | None:
    """Whether an MSBuild condition holds when `$(TargetFramework)` is `framework`, comparing with `==` and `!=` as
    MSBuild does, without regard to case, and StartsWith with regard to it; None when the condition names another
    property or function, is malformed, or the framework is a restore's long name, such as
    `.NETFramework,Version=v4.7.2`, which no short name compares with."""
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
    try:
        value = parser.disjunction()
    except Malformed:
        return None
    return value if parser.position == len(tokens) else None


def conditioned(
    element: Element, entries: dict[Element, ChooseEntry], conditions: Conditions = ()
) -> Iterator[ConditionedElement]:
    """Every descendant outside a Target in document order with the conditions it sits under, its own included. A Choose
    applies its first When whose condition holds, as MSBuild evaluates it, so a When also needs every When before it in
    its Choose to fail, and an Otherwise needs them all to; a When after one that cannot be evaluated is not evaluated
    either, and a When with no Condition, which MSBuild refuses, cannot be evaluated. A Target's contents are defined
    only when it runs, so they are left out (`target_items`). The conditions are evaluated later, for one framework: an
    item's and its ItemGroup's as in the build of that framework, which reads the property's final value, and a When's
    with the value `$(TargetFramework)` held when MSBuild entered its Choose (`entries`, from `FrameworkProperties`),
    so a Choose chooses its branch once, for its items as for its properties."""
    whens: list[str] = []
    entry = entries.get(element)
    for child in element:
        name = local_name(child)
        if name == "Target":
            continue
        own = (child.get("Condition") or "").strip()
        inner: Conditions = (*conditions, (own, True, None)) if own else conditions
        if name in ("When", "Otherwise"):
            inner = (*conditions, *((when, False, entry) for when in whens))
            if name == "When":
                inner = (*inner, (own, True, entry))
                whens.append(own)
        yield child, inner
        yield from conditioned(child, entries, inner)


def written_items(root: Element, kind: str) -> Iterator[tuple[str, str, Element, Conditions]]:
    """Each item named `kind` outside a Target, one per name its Include, Update, or Remove lists, in document order,
    with its operation, its element, and the conditions it sits under."""
    for element, conditions in conditioned(root, FrameworkProperties(root).choose_entries()):
        if local_name(element) != kind:
            continue
        operation = next((name for name in OPERATIONS if element.get(name)), None)
        if operation is not None:
            for name in item_specs(element.get(operation) or ""):
                yield operation, name, element, conditions


def target_items(root: Element, kinds: Sequence[str]) -> list[TargetItem]:
    """Each item of `kinds` inside a Target, one per name it lists, which evaluation leaves out."""
    found: list[TargetItem] = []
    for target in root.iter():
        if local_name(target) != "Target":
            continue
        for element in target.iter():
            kind = local_name(element)
            operation = next((name for name in OPERATIONS if element.get(name)), None)
            if kind in kinds and operation is not None:
                label = target.get("Name") or "without a Name"
                found += [TargetItem(kind, name, label) for name in item_specs(element.get(operation) or "")]
    return found


def evaluation_root(path: Path) -> Element:
    """The root of the MSBuild file at `path`, `Unreadable` when it cannot be read or MSBuild refuses its Choose."""
    root = parse_xml(path)
    if any(local_name(element) == "When" and not (element.get("Condition") or "").strip() for element in root.iter()):
        raise Unreadable(path, "a When has no Condition, which MSBuild refuses, so the file does not evaluate")
    return root


def package_items(root: Element, kind: str = "PackageReference", origin: str = "PackageReference") -> list[PackageItem]:
    """Each PackageReference item, or of another `kind` such as PackageVersion with its `origin`, in document order,
    one per package its Include, Update, or Remove lists, with its Version attribute or child, or a PackageReference's
    VersionOverride, and the conditions it sits under."""
    found: list[PackageItem] = []
    for operation, package, element, conditions in written_items(root, kind):
        version = element.get("Version") or child_text(element, "Version") or element.get("VersionOverride")
        found.append(PackageItem(operation, package, None if operation == "Remove" else version, origin, conditions))
    return found


def conditions_apply(conditions: Conditions, framework: str) -> bool | str:
    """Whether every condition is as it must be in the build of `framework`, where `$(TargetFramework)` is `framework`
    but at a Choose, whose Whens read the value it held when MSBuild entered the Choose (`ChooseEntry`): False when one
    is not, else the text of the first that cannot be evaluated, else True."""
    unevaluated: str | None = None
    for text, wanted, entry in conditions:
        value = condition_at(text, framework if entry is None else entry.framework_at(framework))
        if value is None:
            unevaluated = text if unevaluated is None else unevaluated
        elif value != wanted:
            return False
    return True if unevaluated is None else unevaluated


def condition_at(text: str, framework: str | None) -> bool | None:
    """Whether a condition holds when `$(TargetFramework)` is `framework` (`condition_holds`); one that reads the
    property cannot be evaluated when `framework` is None, as when the property may hold one of several values."""
    if framework is None:
        return None if FRAMEWORK_REFERENCE.search(text) else condition_holds(text, "")
    return condition_holds(text, framework)


Item = TypeVar("Item", PackageItem, ReferenceItem, ProjectItem)
# An item not applied because a condition cannot be evaluated: the item, the condition, and the framework.
Unevaluated = tuple[Item, str, str]


def removed_everywhere(conditions: Conditions, named: Sequence[str]) -> tuple[bool | str, str]:
    """Whether a Remove under `conditions` holds for every framework a project names, as `conditions_apply` says for
    each, with the framework that decides it: one with no condition holds; one with a condition fails when the
    project names no framework or the condition fails for one, else is the condition that cannot be evaluated for
    the first framework it cannot be, else holds."""
    if not conditions:
        return True, ""
    if not named:
        return False, ""
    unevaluated: tuple[str, str] | None = None
    for name in named:
        value = conditions_apply(conditions, name)
        if value is False:
            return False, name
        if isinstance(value, str) and unevaluated is None:
            unevaluated = (value, name)
    return unevaluated or (True, "")


def evaluated(
    items: Sequence[Item],
    framework: str | None,
    key: Callable[[Item], str],
    update: Callable[[Item, Item], Item],
    named: Sequence[str] = (),
) -> tuple[list[Item], list[Unevaluated[Item]]]:
    """The items left under a framework, read in document order as MSBuild evaluates them: an Include adds one, an
    Update changes each included before it under the same key as `update` says, and a Remove drops them; an Update
    or a Remove never adds one. An item whose conditions hold is applied, one whose conditions fail is not, and one
    whose condition cannot be evaluated is not applied and is returned with that condition and the framework. With no
    framework, as for a project with no restore, whose one set of references stands for every framework it builds,
    every Include and Update is applied whatever its condition, and a Remove only where it holds for every framework
    the project names (`removed_everywhere`), so what one framework removes is kept for the others; a Remove that
    fails for none of them but cannot be evaluated for one is not applied and is returned with that framework."""
    found: list[Item] = []
    unevaluated: list[Unevaluated[Item]] = []
    for item in items:
        applies: bool | str
        deciding = framework or ""
        if framework is not None:
            applies = conditions_apply(item.conditions, framework)
        elif item.operation == "Remove":
            applies, deciding = removed_everywhere(item.conditions, named)
        else:
            applies = True
        if isinstance(applies, str):
            unevaluated.append((item, applies, deciding))
            continue
        if not applies:
            continue
        name = key(item)
        if item.operation == "Include":
            found.append(item)
        elif item.operation == "Update":
            found = [update(earlier, item) if key(earlier) == name else earlier for earlier in found]
        else:
            found = [earlier for earlier in found if key(earlier) != name]
    return found, unevaluated


def package_key(item: PackageItem) -> str:
    return item.id.casefold()


def with_version(earlier: PackageItem, update: PackageItem) -> PackageItem:
    """A package item an Update gives its version, when it states one."""
    return replace(earlier, version=update.version, origin=update.origin) if update.version else earlier


def packages_for(
    items: list[PackageItem], framework: str | None, central: Sequence[PackageItem] = (), named: Sequence[str] = ()
) -> tuple[list[PackageRef], list[Unevaluated[PackageItem]]]:
    """The package references the items leave under a framework (`evaluated`, with the frameworks the project
    `named` when there is none), an Include that states no version taking the version the central PackageVersion
    items give it under that framework, with the items whose condition cannot be evaluated: the package items, and
    the central items for a package an Include left without a version."""
    versions, central_unevaluated = evaluated(central, framework, package_key, with_version, named)
    central_versions = {item.id.casefold(): item.version for item in versions if item.version}
    found, unevaluated = evaluated(items, framework, package_key, with_version, named)
    references: list[PackageRef] = []
    for item in found:
        version = item.version or central_versions.get(item.id.casefold())
        references.append(PackageRef(item.id, version, item.origin if item.version or not version else "central"))
    unversioned = {item.id.casefold() for item in found if not item.version}
    return references, [*unevaluated, *(entry for entry in central_unevaluated if package_key(entry[0]) in unversioned)]


def reference_items(root: Element) -> list[ReferenceItem]:
    """Each old-style Reference item, one per assembly its Include, Update, or Remove lists, with its HintPath and
    the conditions it sits under."""
    return [
        ReferenceItem(operation, spec, child_text(element, "HintPath"), conditions)
        for operation, spec, element, conditions in written_items(root, "Reference")
    ]


def with_hint_path(earlier: ReferenceItem, update: ReferenceItem) -> ReferenceItem:
    """A Reference an Update gives its HintPath, when it states one."""
    return replace(earlier, hint_path=update.hint_path) if update.hint_path else earlier


def assemblies_for(
    items: list[ReferenceItem], framework: str | None, named: Sequence[str] = ()
) -> tuple[list[AssemblyRef], list[Unevaluated[ReferenceItem]]]:
    """The assembly references the Reference items leave under a framework (`evaluated`, with the frameworks the
    project `named` when there is none): the name before the first comma, the Version= in the Include, and the
    HintPath; with the items whose condition cannot be evaluated."""
    found, unevaluated = evaluated(items, framework, lambda item: item.spec.casefold(), with_hint_path, named)
    references: list[AssemblyRef] = []
    for item in found:
        match = ASSEMBLY_VERSION.search(item.spec)
        references.append(
            AssemblyRef(item.spec.split(",", 1)[0].strip(), match.group(1) if match else None, item.hint_path)
        )
    return references, unevaluated


def project_items(root: Element, project_dir: Path) -> list[ProjectItem]:
    """Each ProjectReference item, one per project its Include, Update, or Remove lists, resolved against the
    project's directory, with the conditions it sits under."""
    return [
        ProjectItem(operation, project_path(project_dir, name), conditions)
        for operation, name, _, conditions in written_items(root, "ProjectReference")
    ]


def project_refs_for(
    items: list[ProjectItem], framework: str | None, named: Sequence[str] = ()
) -> tuple[list[Path], list[Unevaluated[ProjectItem]]]:
    """The projects the ProjectReference items leave under a framework (`evaluated`, with the frameworks the project
    `named` when there is none), with the items whose condition cannot be evaluated."""
    found, unevaluated = evaluated(
        items, framework, lambda item: same_path(item.path), lambda earlier, _: earlier, named
    )
    return [item.path for item in found], unevaluated


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


def read_central_versions(path: Path) -> CentralVersions:
    """The PackageVersion items of a Directory.Packages.props, with the origin `central`, and those inside a Target;
    an unreadable file is Unreadable."""
    root = evaluation_root(path)
    return CentralVersions(package_items(root, CENTRAL_ITEM, "central"), target_items(root, (CENTRAL_ITEM,)))


def read_project(path: Path, central: Callable[[Path], Sequence[PackageItem]]) -> ProjectFile:
    """What the .csproj at `path` says about its dependencies. `central` gives an SDK-style project's central
    PackageVersion items from the project's directory, so the caller reads each Directory.Packages.props once and
    reports an unreadable one once, without costing the project its own facts."""
    root = evaluation_root(path)
    style = "sdk" if is_sdk_style(root) else "legacy"
    versions = list(central(path.parent)) if style == "sdk" else []
    items = package_items(root)
    references = reference_items(root)
    projects = project_items(root, path.parent)
    frameworks = frameworks_of(root)
    return ProjectFile(
        path=path,
        style=style,
        assembly_name=child_text_anywhere(root, "AssemblyName") or path.stem,
        frameworks=frameworks,
        project_refs=project_refs_for(projects, None, frameworks)[0],
        packages=packages_for(items, None, versions, frameworks)[0],
        assemblies=assemblies_for(references, None, frameworks)[0],
        package_items=items,
        central_items=versions,
        reference_items=references,
        project_items=projects,
        in_targets=target_items(root, PROJECT_ITEMS),
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
    no root with that project is taken as written."""
    parts = recorded.replace("\\", "/").split("/")
    base = restored_project.replace("\\", "/").split("/")[:-1]
    shared = 0
    while shared < min(len(base), len(parts) - 1) and parts[shared].casefold() == base[shared].casefold():
        shared += 1
    if shared == 0:
        return Path("/".join(parts)).resolve()
    return (project_dir / "/".join([*[".."] * (len(base) - shared), *parts[shared:]])).resolve()
