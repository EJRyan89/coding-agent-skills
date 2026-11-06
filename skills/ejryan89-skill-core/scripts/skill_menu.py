"""The menu a skill source offers, and the Claude Code plugins it makes from that menu.

A source's menu items are its bundles, from source.json, and each selectable skill outside a bundle. An item is opt-in
when its bundle or its skill's metadata says so, and selecting it brings its members and every skill their skill_deps
reach. The deployer selects from the menu through expand, root_names, and is_opt_in; tools/plugin_build.py assembles
the plugins plugin_plan names, and tools/skill_reference.py prints their membership in docs/skills.md. It lives in
ejryan89-skill-core because audit-skill's script says, for a skill audited in its source tree, which plugins carry it or
why none does, and a skill's script imports nothing from the deployer or tools/.

A plugin has no deployer, so each template value a skill takes has the fixed text PLUGIN_VALUES gives it, and a menu
item is left out of every plugin when a skill it needs takes a value with no plugin answer: a plugin carries nothing it
cannot run as the deployer would. The source's default selection, every menu item but the opt-in ones, is the plugin
named for the source's repository, the part of its ID after the slash; each opt-in item is a plugin of its own,
`<plugin>-<item>`, so it stays opt-in.

The deployer reads a source through its own loader, which refuses a malformed menu. read_menu is the reader for code
that cannot import the deployer: it reads source.json and deploy-meta/ as that loader does, takes what a well-formed
source says, and fails on what names no skill or cannot be read at all, a list of names of another kind included.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import file_presence
import json_reader

# What each template value becomes in a plugin, which has no deployer to render it. A plugin is one tree for every
# platform, so the interpreter is the command the deployer renders on Windows, and the plugin's README states that it
# must start Python. The repositories root is the plugin option the user sets at install, which Claude Code
# substitutes as typed, and repo-cleanup's optional additions are none, as a deployment's are by default. A value
# missing here has no plugin answer, and a skill that takes one stays out of every plugin.
PLUGIN_VALUES: dict[str, str] = {
    "PYTHON": "python",
    "REPOS_ROOT": "${user_config.repos_root}",
    "REPO_CLEANUP_REGENERABLE": "",
    "REPO_CLEANUP_PROTECTED": "",
}
# Each plugin option a plugin value reads, as the manifest's userConfig of a plugin whose skills take it declares it.
PLUGIN_OPTIONS: dict[str, dict[str, object]] = {
    "repos_root": {
        "type": "directory",
        "title": "Repositories root",
        "description": "The absolute path of the folder holding your git repositories, which repo-cleanup sweeps. "
        "It is used as typed.",
        "required": True,
    },
}
# Claude Code substitutes a plugin option in a skill's body but not in its frontmatter, where the model would read
# the placeholder itself, so a value there that is a plugin option takes these words instead.
PLUGIN_FRONTMATTER_VALUES: dict[str, str] = {"REPOS_ROOT": "your repositories root"}
NO_VALUE = "{skill} takes {{{{{variable}}}}}, which a plugin has no value for"
# Why no plugin carries a skill that no menu item needs, such as a hidden skill nothing depends on.
NO_ITEM = "no menu item needs it"
SOURCE_FILE = "source.json"
# Where a plugin keeps its manifest and the hooks it runs in every session while it is enabled, as Claude Code reads a
# plugin's root: tools/plugin_build.py writes them, and audit-skill's script finds a plugin and its hooks by them. A
# source that makes plugins lists them in its marketplace file.
PLUGIN_MANIFEST = ".claude-plugin/plugin.json"
PLUGIN_HOOKS = "hooks/hooks.json"
PLUGIN_MARKETPLACE = ".claude-plugin/marketplace.json"
METADATA_FOLDER = "deploy-meta"


class MenuError(ValueError):
    """A source tree whose menu cannot be read."""


class MenuSkill(Protocol):
    """What the menu needs of a skill: its dependencies, its template values, and how the menu offers it."""

    @property
    def skill_deps(self) -> Sequence[str]: ...

    @property
    def required_vars(self) -> Sequence[str]: ...

    @property
    def selectable(self) -> bool: ...

    @property
    def opt_in(self) -> bool: ...


class Menu(Protocol):
    """What the menu needs of a source: its ID, its skills, and its bundles."""

    @property
    def source_id(self) -> str: ...

    @property
    def skills(self) -> Mapping[str, MenuSkill]: ...

    @property
    def bundles(self) -> Mapping[str, Sequence[str]]: ...

    @property
    def skill_bundle(self) -> Mapping[str, str]: ...

    @property
    def opt_in_bundles(self) -> AbstractSet[str]: ...


@dataclass(frozen=True)
class MenuEntry:
    """A skill as read_menu reads its metadata."""

    skill_deps: tuple[str, ...] = ()
    required_vars: tuple[str, ...] = ()
    selectable: bool = True
    opt_in: bool = False


@dataclass
class SourceMenu:
    """A source's menu as read_menu reads it."""

    source_id: str
    skills: dict[str, MenuEntry] = field(default_factory=dict)
    bundles: dict[str, list[str]] = field(default_factory=dict)
    skill_bundle: dict[str, str] = field(default_factory=dict)
    opt_in_bundles: set[str] = field(default_factory=set)


def closure(roots: Iterable[str], skill_deps: Callable[[str], Iterable[str]]) -> list[str]:
    """The roots and every skill their skill_deps reach, in depth-first preorder: each skill once, where it is first
    reached. It is the one walk of skill dependencies, which the deployer's selection, audit-skill's declared
    dependencies, and change-skill's owed gates share; `skill_deps` gives a skill's dependencies however its caller
    reads them. The walk keeps its own stack, so a long chain of dependencies cannot exhaust Python's recursion."""
    ordered: list[str] = []
    seen: set[str] = set()
    stack = list(reversed(list(roots)))
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        ordered.append(name)
        stack.extend(reversed(list(skill_deps(name))))
    return ordered


def expand(menu: Menu, bundles: Sequence[str], skills: Sequence[str]) -> list[str]:
    """Depth-first preorder expansion of bundle members, then skills, through dependencies."""
    roots = [*(member for bundle in bundles for member in menu.bundles[bundle]), *skills]
    return closure(roots, lambda name: menu.skills[name].skill_deps)


def root_names(menu: Menu) -> list[str]:
    """Selectable skills that are not part of a bundle, which the menu offers on their own."""
    return sorted(name for name, skill in menu.skills.items() if skill.selectable and name not in menu.skill_bundle)


def is_opt_in(menu: Menu, root: str) -> bool:
    """Whether a menu item, a bundle or a root skill, is left out of --all unless chosen."""
    return root in menu.opt_in_bundles or (root in menu.skills and menu.skills[root].opt_in)


def members(menu: Menu, item: str) -> list[str]:
    """A menu item's members: a bundle's, or the root skill itself."""
    return list(menu.bundles.get(item, [item]))


def plugin_name(source_id: str) -> str:
    """The default plugin's name: the repository part of the source's ID, which every component is namespaced under."""
    return source_id.split("/", 1)[1]


def blocker(menu: Menu, closure: Sequence[str]) -> str | None:
    """Why a plugin cannot carry a menu item with this closure, or None when it can."""
    for name in closure:
        for variable in menu.skills[name].required_vars:
            if variable not in PLUGIN_VALUES:
                return NO_VALUE.format(skill=name, variable=variable)
    return None


def plugin_plan(menu: Menu) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Each plugin a source makes, with the skills it carries, in the order they are assembled: the default
    selection's, then one per opt-in menu item; and each menu item no plugin carries, with why."""
    left_out: dict[str, str] = {}
    default: list[str] = []
    opt_in: list[str] = []
    for item in [*sorted(menu.bundles), *root_names(menu)]:
        closure = expand(menu, [], members(menu, item))
        reason = blocker(menu, closure)
        if reason is not None:
            left_out[item] = reason
        elif is_opt_in(menu, item):
            opt_in.append(item)
        else:
            default.extend(name for name in closure if name not in default)
    name = plugin_name(menu.source_id)
    closures = {name: default} if default else {}
    closures.update({f"{name}-{item}": expand(menu, [], members(menu, item)) for item in opt_in})
    return closures, left_out


def carriers(menu: Menu, skill: str) -> tuple[list[str], dict[str, str]]:
    """The plugins that carry a skill; when none does, each menu item that needs it with why no plugin carries that
    item, or the skill itself with NO_ITEM when no menu item needs it."""
    closures, left_out = plugin_plan(menu)
    carried = [plugin for plugin, closure in closures.items() if skill in closure]
    if carried:
        return carried, {}
    reasons = {item: reason for item, reason in left_out.items() if skill in expand(menu, [], members(menu, item))}
    return [], reasons or {skill: NO_ITEM}


def _json_object(path: Path) -> dict[str, object]:
    try:
        document = json_reader.read_file(path)
    except (OSError, UnicodeError, ValueError) as exc:
        raise MenuError(f"{path.as_posix()} cannot be read: {exc}") from exc
    if not isinstance(document, dict):
        raise MenuError(f"{path.as_posix()} is not a JSON object")
    return document


def _names(value: object, where: str) -> tuple[str, ...]:
    """A list of names as the deployer's loader takes one; any other kind, or a list holding anything but names, is a
    MenuError, since the names it was meant to hold cannot be known and reading it as none would plan a menu the
    deployer refuses."""
    if not isinstance(value, list) or not all(isinstance(name, str) for name in value):
        raise MenuError(f"{where} is not a list of names")
    return tuple(value)


def _flag(value: object, where: str) -> bool:
    """A true-or-false setting as the deployer's loader takes one; any other value, such as the text "true", is a
    MenuError, since read as its default it would plan a selection the deployer refuses."""
    if not isinstance(value, bool):
        raise MenuError(f"{where} is not true or false")
    return value


def _entry(path: Path, metadata: dict[str, object]) -> MenuEntry:
    where = path.as_posix()
    return MenuEntry(
        skill_deps=_names(metadata.get("skill_deps", []), f"{where} skill_deps"),
        required_vars=_names(metadata.get("required_vars", []), f"{where} required_vars"),
        selectable=_flag(metadata.get("selectable", True), f"{where} selectable"),
        opt_in=_flag(metadata.get("opt_in", False), f"{where} opt_in"),
    )


def _read_bundles(menu: SourceMenu, path: Path, value: object) -> None:
    if not isinstance(value, dict):
        raise MenuError(f"{path.as_posix()} bundles is not a JSON object")
    for bundle, definition in sorted(value.items()):
        if not isinstance(definition, dict):
            raise MenuError(f"{path.as_posix()} bundle {bundle} is not a JSON object")
        found = _names(definition.get("members"), f"{path.as_posix()} bundle {bundle} members")
        menu.bundles[str(bundle)] = list(found)
        menu.skill_bundle.update({member: str(bundle) for member in found})
        if _flag(definition.get("opt_in", False), f"{path.as_posix()} bundle {bundle} opt_in"):
            menu.opt_in_bundles.add(bundle)


def read_menu(root: Path) -> SourceMenu:
    """The menu of the source tree at root: its ID and bundles from source.json, and each skill's from its metadata."""
    document = _json_object(root / SOURCE_FILE)
    source_id = document.get("id")
    if not isinstance(source_id, str) or "/" not in source_id:
        raise MenuError(f"{(root / SOURCE_FILE).as_posix()} names no source ID of the form <owner>/<repository>")
    menu = SourceMenu(source_id)
    for path in file_presence.matching(root / METADATA_FOLDER, "*.json"):
        menu.skills[path.stem] = _entry(path, _json_object(path))
    _read_bundles(menu, root / SOURCE_FILE, document.get("bundles", {}))
    named = [*(name for entry in menu.skills.values() for name in entry.skill_deps), *menu.skill_bundle]
    unknown = sorted({name for name in named if name not in menu.skills})
    if unknown:
        raise MenuError(f"{root.as_posix()} names skills it has no metadata for: {', '.join(unknown)}")
    return menu
