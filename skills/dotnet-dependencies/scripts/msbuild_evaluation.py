"""How MSBuild evaluates a project, read as one model: the properties in import order, then the items with the final
properties, once for each build of the project.

The property pass reads the project's files in the order MSBuild imports them, each element where it sits. An SDK-style
project begins with the SDK's props, which import the nearest `Directory.Build.props` at or above the project and then
the nearest `Directory.Packages.props` up to the solution's folder (NuGet's props, from `Microsoft.Common.props`), so
both come before the project's body; it ends with the SDK's targets, which import the nearest `Directory.Build.targets`
and turn each `GlobalPackageReference` into a `PackageReference`. A project that is not SDK-style meets the same points
where it imports `Microsoft.Common.props` and its language's targets. Every other `Import` is read where it sits,
resolved against the importing file's folder, and an `ImportGroup`'s condition applies to each import in it. An import
from the toolset's folders (`$(MSBuildExtensionsPath)`, `$(MSBuildToolsPath)`, `$(VSToolsPath)`, and the like) or of an
SDK sets no property the reader reads, so it is not read. An import the reader does not read for any other reason, a
condition or a path it cannot settle, a file outside the examined tree, missing, or unreadable, is reported, and from it
on every property is unsettled, since the file may set any. A file imported twice is read once, as MSBuild reads it.

Within the files, properties are evaluated top to bottom: a `PropertyGroup`'s condition when the group is reached, a
property's own when the property is, and a `Choose` once, when it is reached, against the values at that point: its
first `When` that holds, else its `Otherwise`. A `When` after one that cannot be settled is read as one of the branches
that may apply; each is read from the values the `Choose` was entered with, and after it a property may hold any value
one of them leaves, or the entry value when none surely applies. A property whose condition cannot be settled may keep
its value or take the new one. `TargetFramework`, `TargetFrameworks`, `TargetFrameworkVersion`, and `AssemblyName` are
unset until a file sets them; any other property no file has set may come from the environment, the command line, or
the toolset, and is unsettled. `$(MSBuildProjectDirectory)`, `$(MSBuildThisFileDirectory)`, and the other names of
the project and the current file are known, and so are `$(SolutionDir)` and its siblings when a solution is built.

The item pass then reads every item the property pass reached, in the same order, with the final properties: an
`ItemGroup`'s condition and an item's own, and an item in a branch a `Choose` may not have taken is unsettled with that
`When`'s condition. An `Include` adds an item, an `Update` changes the metadata of each included before it with that
name, and a `Remove` drops them. Item types and metadata names are matched without regard to case, as MSBuild matches
them; the element names MSBuild treats as keywords and the `Include`, `Update`, and `Remove` attributes are not. A
metadata value is an attribute or a child element, the last child whose condition holds. An item inside a `Target` is
defined only when that target runs, so evaluation leaves it out and reports it.

A project with `TargetFrameworks` builds each framework it names apart, with `$(TargetFramework)` fixed to that
framework from the start, as a global property no file changes; a project with only `TargetFramework` builds once with
the value its files give it; and a project that is not SDK-style names its framework by `TargetFrameworkVersion`, while
its `$(TargetFramework)` stays unset. So each build is one evaluation (`ProjectEvaluation.build`), whose conditions,
`Choose` branches, and imports all read the same values, and the view of a project with no restore is read from the
evaluations of its builds (`merged_steps`): an `Include` or an `Update` applies when it may apply in any build, and a
`Remove` only when it applies in every one, so what one framework removes is kept for the others, and a `Remove`
unsettled in one build and failing in none is kept and reported.

What a condition and a property reference may hold is `msbuild_conditions`' docstring; anything else is unsettled and
reported, never guessed. Each project is evaluated once per build, and each file parsed once per run (`Sources`).
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree.ElementTree import Element

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

from dotnet_solution import project_path, refusal, same_path
from msbuild_conditions import EMPTY, UNKNOWN, FileSystem, Value, condition_value, expanded, union
from msbuild_reader import (
    ASSEMBLY_VERSION,
    CENTRAL_VERSIONS,
    AssemblyRef,
    PackageRef,
    Redirect,
    Unreadable,
    central_versions_file,
    is_sdk_style,
    item_specs,
    local_name,
    matching_framework,
    nearest_file,
    parse_xml,
)

BUILD_PROPS = "Directory.Build.props"
BUILD_TARGETS = "Directory.Build.targets"
OPERATIONS = ("Include", "Update", "Remove")
# The item types the graph reads, by their names case-folded, as MSBuild matches an item type.
ITEM_KINDS = {
    name.casefold(): name
    for name in ("PackageReference", "PackageVersion", "GlobalPackageReference", "ProjectReference", "Reference")
}
# The metadata each item type's facts are read from.
METADATA = {
    "PackageReference": ("Version", "VersionOverride"),
    "PackageVersion": ("Version",),
    "GlobalPackageReference": ("Version",),
    "ProjectReference": (),
    "Reference": ("HintPath",),
}
# The point where NuGet's targets add a PackageReference for each GlobalPackageReference.
GLOBAL_PACKAGES = "GlobalPackageReference to PackageReference"
# The properties unset until a file sets them; any other a file has not set is unsettled.
INITIALLY_EMPTY = frozenset(("targetframework", "targetframeworks", "targetframeworkversion", "assemblyname"))
TOOLSET_PROPERTIES = (
    "MSBuildExtensionsPath",
    "MSBuildExtensionsPath32",
    "MSBuildExtensionsPath64",
    "MSBuildToolsPath",
    "MSBuildToolsPath32",
    "MSBuildToolsPath64",
    "MSBuildBinPath",
    "MSBuildSDKsPath",
    "MSBuildFrameworkToolsPath",
    "MSBuildFrameworkToolsPath32",
    "MSBuildFrameworkToolsPath64",
    "MSBuildToolsRoot",
    "VSToolsPath",
)
TOOLSET_IMPORT = re.compile(r"^\s*\$\(\s*(?:" + "|".join(TOOLSET_PROPERTIES) + r")\s*\)", re.IGNORECASE)
# The toolset files whose import is where Directory.Build.props and Directory.Packages.props are imported, and those
# whose import is where Directory.Build.targets is.
PROPS_FILES = frozenset(("microsoft.common.props", "sdk.props"))
TARGETS_FILES = frozenset(
    ("microsoft.common.targets", "microsoft.csharp.targets", "microsoft.visualbasic.targets", "sdk.targets")
)
# What the reader does instead of applying an item it cannot settle, by the item's operation.
NOT_APPLIED = {"Include": "it is left out", "Update": "the Update is not applied", "Remove": "it is not removed"}

# Whether an item, or a metadata value, applies: True, False, or the condition that cannot be settled.
Decision = bool | str


def evaluation_root(path: Path) -> Element:
    """The root of the MSBuild file at `path`, `Unreadable` when it cannot be read or MSBuild refuses its Choose."""
    root = parse_xml(path)
    if any(local_name(element) == "When" and not (element.get("Condition") or "").strip() for element in root.iter()):
        raise Unreadable(path, "a When has no Condition, which MSBuild refuses, so the file does not evaluate")
    return root


@dataclass
class Sources:
    """The files one run evaluates its projects from, each read and parsed once, and what MSBuild gives every project
    of the run: the examined tree no file is read outside (`refusal`), the folder the search for
    Directory.Packages.props stops at, and the solution, whose `$(SolutionDir)` and siblings a solution build sets."""

    tree: Path
    base: Path
    solution: Path | None = None
    roots: dict[str, Element | Unreadable] = field(default_factory=dict)

    def root(self, path: Path) -> Element:
        key = same_path(path)
        if key not in self.roots:
            try:
                self.roots[key] = evaluation_root(path)
            except Unreadable as error:
                self.roots[key] = error
        found = self.roots[key]
        if isinstance(found, Unreadable):
            raise Unreadable(found.file, found.reason)
        return found

    def solution_properties(self) -> dict[str, str | None]:
        if self.solution is None:
            return {}
        return {
            "solutiondir": f"{self.solution.parent.as_posix()}/",
            "solutionpath": self.solution.as_posix(),
            "solutionname": self.solution.stem,
            "solutionfilename": self.solution.name,
            "solutionext": self.solution.suffix,
        }


def file_properties(path: Path, prefix: str) -> dict[str, str | None]:
    """The names MSBuild gives a file, as `MSBuildProject...` for the project or `MSBuildThisFile...` for the file being
    read, keyed case-folded."""
    names = {
        "directory": f"{path.parent.as_posix()}/" if prefix == "msbuildthisfile" else path.parent.as_posix(),
        "fullpath": path.as_posix(),
        "extension": path.suffix,
        "name": path.stem,
    }
    found: dict[str, str | None] = {f"{prefix}{key}": value for key, value in names.items()}
    found["msbuildthisfile" if prefix == "msbuildthisfile" else "msbuildprojectfile"] = path.name
    return found


class State:
    """The values of the properties at one point of an evaluation, each the values it may hold (`Value`), keyed by its
    name case-folded. `fixed` holds the global and reserved properties, which no file changes; once an import the reader
    did not read may have set any property, every property not set since is unsettled (`opaque`)."""

    def __init__(self, fixed: dict[str, str | None], values: dict[str, Value] | None = None, opaque: bool = False):
        self.fixed = fixed
        self.values = values or {}
        self.opaque = opaque

    def get(self, name: str) -> Value:
        key = name.casefold()
        if key in self.fixed:
            return (self.fixed[key],)
        if key in self.values:
            return self.values[key]
        return UNKNOWN if self.opaque or key not in INITIALLY_EMPTY else EMPTY

    def set(self, name: str, value: Value, certain: bool) -> None:
        key = name.casefold()
        if key not in self.fixed:
            self.values[key] = value if certain else union(self.get(key), value)

    def copy(self) -> State:
        return State(self.fixed, dict(self.values), self.opaque)

    def unsettle(self) -> None:
        self.values = dict.fromkeys(self.values, UNKNOWN)
        self.opaque = True

    def restricted(self, name: str, value: str) -> State:
        """This state in the build where the property ended with `value`."""
        found = self.copy()
        found.values[name.casefold()] = (value,)
        return found

    @staticmethod
    def joined(states: list[State]) -> State:
        """The state after one of several alternatives, not knowing which: each property may hold any value one of
        them leaves it."""
        names = dict.fromkeys(name for state in states for name in state.values)
        values = {name: union(*(state.get(name) for state in states)) for name in names}
        return State(states[0].fixed, values, any(state.opaque for state in states))


@dataclass(frozen=True)
class Placed:
    """An item as the property pass reaches it, in import order: its type, operation, and element, the conditions of
    its ItemGroup and its own, evaluated in the item pass, and the condition of a When it sits in that the property pass
    could not settle. A placed item with no element is where NuGet's targets add the GlobalPackageReference items."""

    kind: str
    operation: str
    element: Element | None
    conditions: tuple[str, ...] = ()
    branch: str | None = None


# A metadata value as written, whether it applies, and the condition it sits under.
Candidate = tuple[str, Decision, str]


@dataclass(frozen=True)
class Resolved:
    """A placed item read in the item pass of one build: the names it lists, None when they cannot be settled, whether
    it applies, and each metadata value it may take."""

    placed: Placed
    names: tuple[str, ...] | None
    decision: Decision
    metadata: dict[str, list[Candidate]]


@dataclass(frozen=True)
class Unsettled:
    """An item, or one of its metadata values, a build did not apply because the condition it sits under, or the name
    it lists, cannot be settled: the item's type, operation, and name (a ProjectReference's project resolved as
    `path`), the condition, `""` for a name, and the framework of the build, `""` for one that names none."""

    kind: str
    operation: str
    name: str
    condition: str
    framework: str
    path: Path | None = None
    metadata: str = ""

    def reason(self, subject: str) -> str:
        """The UNREADABLE reason, naming the item as `subject`."""
        build = f"for {self.framework}" if self.framework else "for a build that names no framework"
        if self.metadata:
            return (
                f"the condition {self.condition} on the {self.metadata} of {self.kind} {subject} cannot be evaluated "
                f"{build}, so that {self.metadata} is not applied"
            )
        item = (
            f"{self.kind} {subject}"
            if self.operation == "Include"
            else f"the {self.operation} of {self.kind} {subject}"
        )
        if not self.condition:
            return f"{item} names a value the reader cannot settle {build}, so {NOT_APPLIED[self.operation]}"
        return f"the condition {self.condition} on {item} cannot be evaluated {build}, so {NOT_APPLIED[self.operation]}"


class Evaluation:
    """One evaluation of a project, as MSBuild runs it for one build: the property pass over its files in import
    order, which places its items (`items`) and leaves the final properties (`state`), and the item pass (`resolved`).
    `framework` is the global `$(TargetFramework)` of a build of one of the frameworks `TargetFrameworks` names, None
    for one the reader cannot name; without `fixed`, the files give the property its value."""

    def __init__(self, project: ProjectEvaluation, fixed: bool = False, framework: str | None = None) -> None:
        self.project = project
        properties = {**file_properties(project.path, "msbuildproject"), **project.sources.solution_properties()}
        if fixed:
            properties["targetframework"] = framework
        self.state = State(properties)
        self.file = project.path
        self.items: list[Placed] = []
        self.reports: list[Unreadable] = []
        self.imported: set[str] = set()
        self.points: set[str] = set()
        self.files = FileSystem(project.path.parent.as_posix(), self.exists, self.folder_above)
        self.run(project.root)

    def value_of(self, name: str) -> Value:
        key = name.casefold()
        if key.startswith("msbuildthisfile"):
            return (file_properties(self.file, "msbuildthisfile").get(key),)
        return self.state.get(key)

    def exists(self, text: str) -> bool | None:
        path = project_path(self.project.path.parent, text)
        return None if refusal(path, self.project.sources.tree) is not None else path.exists()

    def folder_above(self, name: str, start: str) -> str | None:
        directory = project_path(self.project.path.parent, start)
        if refusal(directory, self.project.sources.tree) is not None:
            return None
        found = nearest_file(directory, name)
        return "" if found is None else found.parent.as_posix()

    def holds(self, text: str | None) -> bool | None:
        """Whether a condition holds now; an absent or empty one does."""
        text = (text or "").strip()
        return condition_value(text, self.value_of, self.files) if text else True

    def run(self, root: Element) -> None:
        self.imported.add(same_path(self.project.path))
        implicit = bool(root.get("Sdk")) or any(local_name(child) == "Sdk" for child in root)
        if implicit:
            self.props_point()
        self.walk(root, None)
        if implicit:
            self.targets_point()

    def walk(self, container: Element, branch: str | None) -> None:
        """The property pass over a project, an imported file, a When, or an Otherwise."""
        for child in container:
            name = local_name(child)
            if name == "PropertyGroup":
                self.property_group(child)
            elif name == "ItemGroup":
                self.item_group(child, branch)
            elif name == "Choose":
                self.choose(child, branch)
            elif name == "Import":
                self.import_element(child, ())
            elif name == "ImportGroup":
                self.import_group(child)
            elif name == "Target":
                self.target(child)

    def property_group(self, group: Element) -> None:
        applies = self.holds(group.get("Condition"))
        if applies is False:
            return
        for element in group:
            name = local_name(element)
            own = self.holds(element.get("Condition"))
            if not name or own is False:
                continue
            text = (element.text or "").strip()
            self.state.set(name, expanded(text, self.value_of, self.files), applies is True and own is True)

    def item_group(self, group: Element, branch: str | None) -> None:
        outer = (group.get("Condition") or "").strip()
        for element in group:
            kind = ITEM_KINDS.get(local_name(element).casefold())
            operation = next((name for name in OPERATIONS if element.get(name)), None)
            if kind is None or operation is None:
                continue
            own = (element.get("Condition") or "").strip()
            self.items.append(Placed(kind, operation, element, tuple(text for text in (outer, own) if text), branch))

    def branches(self, choose: Element) -> tuple[list[tuple[Element, Decision]], bool]:
        """The branches of a Choose that may apply, each Whens' condition evaluated now, with whether each surely
        applies or the first condition that leaves it unsettled; and whether one of them surely applies."""
        found: list[tuple[Element, Decision]] = []
        unsettled: str | None = None
        for branch in choose:
            name = local_name(branch)
            text = (branch.get("Condition") or "").strip()
            value = self.holds(text) if name == "When" else True
            if name not in ("When", "Otherwise") or value is False:
                continue
            if value is True:
                found.append((branch, True if unsettled is None else unsettled))
                return found, True
            unsettled = unsettled or text
            found.append((branch, unsettled))
        return found, False

    def choose(self, choose: Element, branch: str | None) -> None:
        found, surely = self.branches(choose)
        if len(found) == 1 and found[0][1] is True:
            self.walk(found[0][0], branch)
            return
        entry = self.state
        states = [] if surely else [entry]
        for element, decision in found:
            self.state = entry.copy()
            self.walk(element, branch or (decision if isinstance(decision, str) else None))
            states.append(self.state)
        self.state = State.joined(states)

    def import_group(self, group: Element) -> None:
        applies = self.holds(group.get("Condition"))
        if applies is False:
            return
        conditions = () if applies is True else ((group.get("Condition") or "").strip(),)
        for element in group:
            if local_name(element) == "Import":
                self.import_element(element, conditions)

    def import_element(self, element: Element, unsettled: tuple[str, ...]) -> None:
        written = element.get("Project") or ""
        if element.get("Sdk") is not None or TOOLSET_IMPORT.match(written):
            name = written.replace("\\", "/").rsplit("/", 1)[-1].strip().casefold()
            if name in PROPS_FILES:
                self.props_point()
            elif name in TARGETS_FILES:
                self.targets_point()
            return
        applies = self.holds(element.get("Condition"))
        if applies is False:
            return
        if unsettled or applies is None:
            condition = (unsettled or ((element.get("Condition") or "").strip(),))[0]
            self.not_read(written, f"its condition {condition} cannot be evaluated")
            return
        values = expanded(written, self.value_of, self.files)
        if len(values) != 1 or values[0] is None:
            self.not_read(written, "its path names a value the reader cannot settle")
        elif "*" in values[0] or "?" in values[0]:
            self.not_read(written, "its path is a wildcard, which the reader does not expand")
        else:
            self.import_file(project_path(self.file.parent, values[0]), written)

    def not_read(self, written: str, why: str) -> None:
        reason = f"the Import of {written} is not read: {why}, so every property is unsettled after it"
        self.reports.append(Unreadable(self.file, reason))
        self.state.unsettle()

    def import_file(self, path: Path, written: str) -> None:
        key = same_path(path)
        if key in self.imported:
            return
        refused = refusal(path, self.project.sources.tree)
        if refused is not None:
            self.not_read(written, refused)
            return
        if not path.is_file():
            self.not_read(written, "the file it names is not found")
            return
        try:
            root = self.project.sources.root(path)
        except Unreadable as error:
            self.reports.append(error)
            self.state.unsettle()
            return
        self.imported.add(key)
        importing, self.file = self.file, path
        self.walk(root, None)
        self.file = importing

    def props_point(self) -> None:
        """Where Microsoft.Common.props imports Directory.Build.props and, through NuGet's props,
        Directory.Packages.props, each once."""
        if "props" in self.points:
            return
        self.points.add("props")
        directory = self.project.path.parent
        found = nearest_file(directory, BUILD_PROPS)
        if found is not None:
            self.import_file(found, BUILD_PROPS)
        central = central_versions_file(directory, self.project.sources.base)
        if central is not None:
            self.import_file(central, CENTRAL_VERSIONS)

    def targets_point(self) -> None:
        """Where the common targets import Directory.Build.targets, and NuGet's add the GlobalPackageReference items."""
        if "targets" in self.points:
            return
        self.points.add("targets")
        found = nearest_file(self.project.path.parent, BUILD_TARGETS)
        if found is not None:
            self.import_file(found, BUILD_TARGETS)
        self.items.append(Placed(GLOBAL_PACKAGES, "Include", None))

    def target(self, target: Element) -> None:
        """Report each item inside a Target, which MSBuild defines only when the target runs."""
        label = target.get("Name") or "without a Name"
        for element in target.iter():
            kind = ITEM_KINDS.get(local_name(element).casefold())
            operation = next((name for name in OPERATIONS if element.get(name)), None)
            if kind is None or operation is None:
                continue
            for name in item_specs(element.get(operation) or ""):
                reason = f"the {kind} {name} inside Target {label} is left out: MSBuild defines it only when that "
                self.reports.append(Unreadable(self.file, reason + "target runs"))

    def resolved(self, restrict: str | None = None) -> list[Resolved]:
        """The item pass: each placed item read with the final properties, `$(TargetFramework)` restricted to
        `restrict` when the build is the one where the files left it that value."""
        state = self.state if restrict is None else self.state.restricted("TargetFramework", restrict)
        self.state, final = state, self.state
        try:
            return [self.resolve(placed) for placed in self.items]
        finally:
            self.state = final

    def resolve(self, placed: Placed) -> Resolved:
        if placed.element is None:
            return Resolved(placed, (), True, {})
        decision: Decision = True
        for text in placed.conditions:
            value = self.holds(text)
            if value is False:
                return Resolved(placed, (), False, {})
            if value is None and decision is True:
                decision = text
        if placed.branch is not None:
            decision = placed.branch
        values = expanded(placed.element.get(placed.operation) or "", self.value_of, self.files)
        names = tuple(item_specs(values[0])) if len(values) == 1 and values[0] is not None else None
        metadata = {name: self.candidates(placed.element, name) for name in METADATA[placed.kind]}
        return Resolved(placed, names, decision, metadata)

    def candidates(self, element: Element, name: str) -> list[Candidate]:
        """Each value a metadata may take, in order: its attribute, then each child of its name, under its condition."""
        found: list[Candidate] = [
            (self.metadata_value(text), True, "")
            for key, text in element.attrib.items()
            if key.casefold() == name.casefold()
        ]
        for child in element:
            if local_name(child).casefold() != name.casefold():
                continue
            condition = (child.get("Condition") or "").strip()
            value = self.holds(condition)
            decision: Decision = condition if value is None else value
            found.append((self.metadata_value((child.text or "").strip()), decision, condition))
        return found

    def metadata_value(self, text: str) -> str:
        """A metadata value with its property references expanded, or as written when they cannot be settled."""
        values = expanded(text, self.value_of, self.files)
        return values[0] if len(values) == 1 and values[0] is not None else text


@dataclass
class Step:
    """One item of the item pass of a build or of the view with no restore: the item, the names it lists, whether it
    applies, the metadata it gives, and the metadata it leaves unsettled with the condition, and the framework
    named for what it leaves unsettled."""

    placed: Placed
    names: tuple[str, ...] | None
    decision: Decision
    metadata: dict[str, str]
    unsettled: list[tuple[str, str]]
    framework: str


def build_step(resolved: Resolved, framework: str) -> Step:
    """A step of one build: each metadata value the last that applies, and one whose condition cannot be settled after
    it named and not applied."""
    metadata: dict[str, str] = {}
    unsettled: list[tuple[str, str]] = []
    for name, candidates in resolved.metadata.items():
        pending: str | None = None
        for value, decision, _ in candidates:
            if decision is True:
                metadata[name], pending = value, None
            elif decision is not False:
                pending = pending or decision
        if pending is not None:
            unsettled.append((name, pending))
    return Step(resolved.placed, resolved.names, resolved.decision, metadata, unsettled, framework)


def merged_decision(placed: Placed, decisions: list[tuple[str, Decision]]) -> tuple[Decision, str]:
    """Whether an item applies in the view with no restore, from its decision in each build, and the framework named
    for one it leaves unsettled: an Include or an Update when it may apply in any build, and a Remove only when it
    applies in every one; a Remove that fails in none and cannot be settled in one is that build's."""
    if placed.operation != "Remove":
        return any(decision is not False for _, decision in decisions), ""
    if any(decision is False for _, decision in decisions):
        return False, ""
    return next(((decision, framework) for framework, decision in decisions if decision is not True), (True, ""))


def merged_steps(builds: list[Build]) -> list[Step]:
    """The steps of the view with no restore: each item any build reaches, in the order the builds reach them, its
    names and metadata read as the first build where it may apply reads them, and a metadata value the last that may
    apply in any build."""
    reached: dict[object, list[tuple[str, Resolved]]] = {}
    order: list[object] = []
    for build in builds:
        position = 0
        for resolved in build.items:
            key = resolved.placed.element if resolved.placed.element is not None else GLOBAL_PACKAGES
            if key in reached:
                position = order.index(key) + 1
            else:
                order.insert(position, key)
                position += 1
            reached.setdefault(key, []).append((build.framework, resolved))
    steps: list[Step] = []
    for present in (reached[key] for key in order):
        seen = {framework for framework, _ in present}
        decisions = [(framework, item.decision) for framework, item in present]
        decisions += [(build.framework, False) for build in builds if build.framework not in seen]
        decision, framework = merged_decision(present[0][1].placed, decisions)
        chosen = next((item for _, item in present if item.decision is not False), present[0][1])
        metadata: dict[str, str] = {}
        for name, candidates in chosen.metadata.items():
            each = [item.metadata.get(name, []) for _, item in present]
            for index, (value, _, _) in enumerate(candidates):
                if any(len(other) > index and other[index][1] is not False for other in each):
                    metadata[name] = value
        steps.append(Step(chosen.placed, chosen.names, decision, metadata, [], framework or present[0][0]))
    return steps


@dataclass
class Entry:
    """An item in an item list: its name, its metadata, and where a PackageReference came from."""

    name: str
    metadata: dict[str, str]
    origin: str


def item_key(kind: str, name: str, directory: Path) -> str:
    """What an Update or a Remove matches an item by: a project by its path, any other by its name without case."""
    return same_path(project_path(directory, name)) if kind == "ProjectReference" else name.casefold()


def step_unsettled(step: Step, directory: Path) -> list[Unsettled]:
    """What a step leaves unsettled: the item when its condition or its names cannot be settled, else its metadata."""
    placed = step.placed
    condition = step.decision if isinstance(step.decision, str) else ""
    if step.names is None or condition:
        names = step.names or (placed.element.get(placed.operation) or "" if placed.element is not None else "",)
        return [
            Unsettled(
                placed.kind, placed.operation, name, condition, step.framework, item_path(placed, name, directory)
            )
            for name in names
        ]
    return [
        Unsettled(placed.kind, placed.operation, name, pending, step.framework, item_path(placed, name, directory), key)
        for key, pending in step.unsettled
        for name in step.names
    ]


def item_path(placed: Placed, name: str, directory: Path) -> Path | None:
    return project_path(directory, name) if placed.kind == "ProjectReference" else None


def item_pass(steps: Iterable[Step], directory: Path) -> tuple[dict[str, list[Entry]], list[Unsettled]]:
    """The item lists the steps leave, in order: an Include adds an item, an Update sets the metadata it gives on each
    included before it with that name, a Remove drops them, and at NuGet's point each GlobalPackageReference becomes a
    PackageReference; with what the steps leave unsettled."""
    lists: dict[str, list[Entry]] = {kind: [] for kind in ITEM_KINDS.values()}
    unsettled: list[Unsettled] = []
    for step in steps:
        kind = step.placed.kind
        if kind == GLOBAL_PACKAGES:
            globals_ = lists["GlobalPackageReference"]
            lists["PackageReference"] += [Entry(item.name, dict(item.metadata), "central") for item in globals_]
            continue
        if step.decision is False:
            continue
        unsettled += step_unsettled(step, directory)
        if step.decision is not True or step.names is None:
            continue
        for name in step.names:
            key = item_key(kind, name, directory)
            if step.placed.operation == "Include":
                lists[kind].append(Entry(name, dict(step.metadata), kind))
            elif step.placed.operation == "Update":
                for item in lists[kind]:
                    if item_key(kind, item.name, directory) == key:
                        item.metadata.update(step.metadata)
            else:
                lists[kind] = [item for item in lists[kind] if item_key(kind, item.name, directory) != key]
    return lists, unsettled


@dataclass
class Build:
    """What one build of a project references: its packages, each without a version of its own at its central
    version, its projects and assemblies, what its item pass left unsettled, a central version's only for a package
    that takes one, and what its evaluation could not read; with its items, for the view with no restore."""

    framework: str
    packages: list[PackageRef] = field(default_factory=list)
    project_refs: list[Path] = field(default_factory=list)
    assemblies: list[AssemblyRef] = field(default_factory=list)
    unsettled: list[Unsettled] = field(default_factory=list)
    reports: list[Unreadable] = field(default_factory=list)
    items: list[Resolved] = field(default_factory=list)


def references(framework: str, steps: Iterable[Step], directory: Path) -> Build:
    """The references the steps of a build, or of the view with no restore, leave."""
    lists, unsettled = item_pass(steps, directory)
    versions = {
        item.name.casefold(): version for item in lists["PackageVersion"] if (version := item.metadata.get("Version"))
    }
    packages: list[PackageRef] = []
    unversioned: set[str] = set()
    for item in lists["PackageReference"]:
        own = item.metadata.get("Version") or item.metadata.get("VersionOverride")
        version = own or versions.get(item.name.casefold())
        central = item.origin == "central" or (not own and version is not None)
        packages.append(PackageRef(item.name, version, "central" if central else "PackageReference"))
        if not own:
            unversioned.add(item.name.casefold())
    unsettled = [entry for entry in unsettled if entry.kind != "PackageVersion" or entry.name.casefold() in unversioned]
    assemblies = [
        AssemblyRef(
            item.name.split(",", 1)[0].strip(),
            match.group(1) if (match := ASSEMBLY_VERSION.search(item.name)) else None,
            item.metadata.get("HintPath"),
        )
        for item in lists["Reference"]
    ]
    projects = [project_path(directory, item.name) for item in lists["ProjectReference"]]
    return Build(framework, packages, projects, assemblies, unsettled)


def framework_names(state: State, style: str) -> tuple[list[str], list[str], bool]:
    """The frameworks a project builds, from its final properties: those `TargetFrameworks` names, each built apart;
    else the one `TargetFramework` names, or, for a project that is not SDK-style, `TargetFrameworkVersion`, built
    once; and whether `TargetFrameworks` may name frameworks the reader cannot settle."""
    several = state.get("TargetFrameworks")
    apart = [part.strip() for value in several if value for part in value.split(";") if part.strip()]
    once: list[str] = []
    if any(not value for value in several):
        single = state.get("TargetFramework")
        once += [value.strip() for value in single if value and value.strip()]
        if style == "legacy" and any(not value for value in single):
            once += [value.strip() for value in state.get("TargetFrameworkVersion") if value and value.strip()]
    return list(dict.fromkeys(apart)), list(dict.fromkeys(once)), None in several


class ProjectEvaluation:
    """A project's evaluations, one per build: the one the files alone give, which names the frameworks and the
    assembly, and one for each framework `TargetFrameworks` names, made when first asked for (`build`)."""

    def __init__(self, path: Path, sources: Sources) -> None:
        self.path = path
        self.sources = sources
        self.root = sources.root(path)
        self.style = "sdk" if is_sdk_style(self.root) else "legacy"
        self.outer = Evaluation(self)
        self.apart, self.once, self.unnamed = framework_names(self.outer.state, self.style)
        self.frameworks = list(dict.fromkeys([*self.apart, *self.once]))
        self.builds: dict[str, Build] = {}

    def build(self, framework: str) -> Build:
        """The build of a framework, `""` for one that names none: of a framework `TargetFrameworks` names, with it
        fixed; of the one the files name, the evaluation they give; and of a framework the project does not name, such
        as a restore's long name, with it fixed, where conditions that read it cannot be settled when it is a long
        name."""
        if framework not in self.builds:
            self.builds[framework] = self.evaluate(framework)
        return self.builds[framework]

    def evaluate(self, framework: str) -> Build:
        restrict: str | None = None
        apart = matching_framework(framework, self.apart) if framework else None
        once = matching_framework(framework, self.once) if framework else None
        if apart is not None:
            evaluation = Evaluation(self, True, apart)
        elif once is not None or (not framework and not self.unnamed):
            evaluation = self.outer
            final = self.outer.state.get("TargetFramework")
            restrict = once if once is not None and len(final) > 1 and once in final else None
        else:
            evaluation = Evaluation(self, True, framework if framework and "," not in framework else None)
        items = evaluation.resolved(restrict)
        found = references(framework, (build_step(item, framework) for item in items), self.path.parent)
        found.items = items
        found.reports = list(evaluation.reports)
        return found

    def labels(self) -> list[str]:
        """The builds the view with no restore reads: one per framework, and one for frameworks the reader cannot
        name, which is the only one when the project names none."""
        found = list(self.frameworks)
        if self.unnamed or not found:
            found.append("")
        return found


@dataclass
class ProjectFile:
    """What one .csproj says about its dependencies, evaluated as MSBuild evaluates it: its references as for a
    project with no restore, which are also the projects loaded as its references, read from the evaluations of every
    build (`merged_steps`), with the Removes and names that view leaves unsettled, what its evaluation could not read,
    and each build's references (`build`)."""

    path: Path
    style: str  # sdk or legacy
    assembly_name: str
    frameworks: list[str]
    project_refs: list[Path]
    packages: list[PackageRef]
    assemblies: list[AssemblyRef]
    redirects: list[Redirect] = field(default_factory=list)
    unsettled: list[Unsettled] = field(default_factory=list)
    reports: list[Unreadable] = field(default_factory=list)
    evaluation: ProjectEvaluation | None = None

    def build(self, framework: str) -> Build:
        """The references of the build of a framework (`ProjectEvaluation.build`), none for a project not read."""
        return Build(framework) if self.evaluation is None else self.evaluation.build(framework)


def assembly_name(evaluation: ProjectEvaluation, reports: list[Unreadable]) -> str:
    """The project's AssemblyName as its files leave it, else its file name, as MSBuild defaults it; one the reader
    cannot settle is reported and the file name used. An import the reader did not read is already reported, so the
    value it may have set is not reported again."""
    state = evaluation.outer.state
    values = [value for value in state.get("AssemblyName") if value is not None or not state.opaque] or [""]
    if values == [""]:
        return evaluation.path.stem
    if len(values) == 1 and values[0]:
        return values[0]
    reason = f"the AssemblyName cannot be settled, so the project is named by its file name {evaluation.path.stem}"
    reports.append(Unreadable(evaluation.path, reason))
    return evaluation.path.stem


def distinct(reports: Iterable[Unreadable]) -> list[Unreadable]:
    """Each report once, by its file and reason, in the order first met."""
    found: dict[tuple[str, str], Unreadable] = {}
    for report in reports:
        found.setdefault((same_path(report.file), report.reason), report)
    return list(found.values())


def read_project(path: Path, sources: Sources) -> ProjectFile:
    """What the .csproj at `path` says about its dependencies, evaluated once per build; `Unreadable` when the file
    itself cannot be read. What an import, a Target, or the AssemblyName costs is in `reports`."""
    evaluation = ProjectEvaluation(path, sources)
    builds = [evaluation.build(label) for label in evaluation.labels()]
    reports = distinct([*evaluation.outer.reports, *(report for build in builds for report in build.reports)])
    name = assembly_name(evaluation, reports)
    view = references("", merged_steps(builds), path.parent)
    return ProjectFile(
        path=path,
        style=evaluation.style,
        assembly_name=name,
        frameworks=evaluation.frameworks,
        project_refs=view.project_refs,
        packages=view.packages,
        assemblies=view.assemblies,
        unsettled=view.unsettled,
        reports=reports,
        evaluation=evaluation,
    )
