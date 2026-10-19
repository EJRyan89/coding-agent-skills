"""Assemble the Claude Code plugins from the skills, their agents, and their metadata, which stay the one source.

Usage:
  python tools/plugin_build.py [--source DIR] [--output DIR] [--archive] [--version VERSION]

It reads a source tree (source.json, deploy-meta/, skills/, and agents/; this checkout by default) as the deployer
does, and writes each plugin as a tree into a new folder under the system temporary directory, or into --output, which
must lie outside the source tree and every skills directory and be absent or empty. With --archive it writes each
plugin as <plugin>.zip there instead: its entries sorted, each stored uncompressed with a fixed time and mode, so one
source always gives the same bytes and anyone can rebuild an archive and compare. It never writes into the source
tree.

A plugin carries what a deployment of the same items would ship: each skill's files but its regression suites, and
each agent the skills declare. The source's default selection, every menu item but the opt-in ones, is the plugin
named for the source's repository, the part of its ID after the slash; each opt-in bundle or skill is a plugin of its
own, `<plugin>-<item>`, so it stays opt-in. A plugin has no deployer, so each template value a skill carries takes the
fixed text PLUGIN_VALUES gives it, and a menu item is left out of every plugin when a skill it needs takes a value
with no plugin answer: a plugin carries nothing it cannot run as the deployer would.

Each plugin is described by its entry in the source's marketplace file, .claude-plugin/marketplace.json, which names
every plugin the source makes and nothing else. Its manifest, .claude-plugin/plugin.json, is that entry without its
source, with the plugin's name and --version (0.0.0-dev unless given; a release passes its tag without the `v`).
Claude Code namespaces a plugin's agents under its name, so a skill's backticked mention of an agent the plugin
carries names it as `<plugin>:<agent>`. A plugin agent's own hooks are ignored, so each agent's hooks move into the
plugin's hooks/hooks.json, as tools/plugin_agents.py describes.

It prints one fact per line:

  OUTPUT "<dir>"                     where the plugins are written
  PLUGIN <plugin> "<path>"           a plugin tree written, or, with --archive:
  ARCHIVE <plugin> "<path>" <sha256> a plugin archive written, with its digest
  SKILL <plugin> <skill>             a skill the plugin carries
  AGENT <plugin> <agent>             an agent the plugin carries
  LEFT_OUT <item> "<reason>"         a menu item no plugin carries, and why
  FAILED <reason>                    the source cannot be read or the output cannot be written; exit 1
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import tempfile
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "ejryan89-skill-core" / "scripts"))

from console import run_main
from flat_text import flat_text
from skill_roots import skills_directory_holding

from deployer import source as deploy_source
from deployer.errors import DeployError
from deployer.paths import Paths
from deployer.render import SUFFIX_CONTEXT, TOKEN, shipped_files
from tools.plugin_agents import AgentError, Node, plugin_agent

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
# What each template value becomes in a plugin, which has no deployer to render it. A plugin is one tree for every
# platform, so the interpreter is the command the deployer renders on Windows, and the plugin's README states that it
# must start Python. A value missing here has no plugin answer, and a skill that takes one stays out of every plugin.
PLUGIN_VALUES: dict[str, str] = {"PYTHON": "python"}
# The fixed time and mode of every archive entry, so an archive's bytes depend on its files alone.
ARCHIVE_TIME = (1980, 1, 1, 0, 0, 0)
ARCHIVE_MODE = 0o100644 << 16
NO_VALUE = "{skill} takes {{{{{variable}}}}}, which a plugin has no value for"
MARKETPLACE = Path(".claude-plugin") / "marketplace.json"
MANIFEST = ".claude-plugin/plugin.json"
HOOKS = "hooks/hooks.json"
DEVELOPMENT_VERSION = "0.0.0-dev"


class BuildError(Exception):
    """A source a plugin cannot be assembled from, or an output it cannot be written to."""


@dataclass
class Plugin:
    """One plugin: its name, the skills and agents it carries, and every file, by its path in the plugin."""

    name: str
    skills: list[str] = field(default_factory=list)
    agents: list[str] = field(default_factory=list)
    files: dict[str, bytes] = field(default_factory=dict)


@dataclass
class Assembly:
    """Every plugin a source makes, and each menu item no plugin carries, with why."""

    plugins: list[Plugin] = field(default_factory=list)
    left_out: dict[str, str] = field(default_factory=dict)


def plugin_name(source_id: str) -> str:
    """The default plugin's name: the repository part of the source's ID, which every component is namespaced under."""
    return source_id.split("/", 1)[1]


def load_source(directory: Path) -> deploy_source.Source:
    paths = Paths(directory, directory)
    try:
        return deploy_source.discover(paths, deploy_source.load_source_id(paths))
    except DeployError as exc:
        raise BuildError(" ".join(exc.lines)) from exc


def _members(source: deploy_source.Source, item: str) -> list[str]:
    return source.bundles.get(item, [item])


def load_catalogue(directory: Path) -> dict[str, dict[str, object]]:
    """The source's marketplace entries by plugin name. A source tree holds no plugin manifest of its own, which
    would make the skills in its checkout read as a plugin's."""
    if (directory / MANIFEST).exists():
        raise BuildError(f"{directory / MANIFEST} exists; a source tree is never a plugin itself")
    try:
        document = json.loads((directory / MARKETPLACE).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BuildError(f"{directory / MARKETPLACE} cannot be read: {exc}") from exc
    plugins = document.get("plugins") if isinstance(document, dict) else None
    entries: dict[str, dict[str, object]] = {}
    for entry in plugins if isinstance(plugins, list) else [None]:
        name = entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(entry, dict) or not isinstance(name, str) or name in entries:
            raise BuildError(f"{directory / MARKETPLACE} must list each plugin once, as an object with a name")
        entries[name] = entry
    return entries


def _blocker(source: deploy_source.Source, closure: list[str]) -> str | None:
    """Why a plugin cannot carry a menu item with this closure, or None when it can."""
    for name in closure:
        for variable in source.skills[name].required_vars:
            if variable not in PLUGIN_VALUES:
                return NO_VALUE.format(skill=name, variable=variable)
    return None


def render(logical: str, content: bytes, variables: list[str]) -> bytes:
    """A file with each variable it may take in its plugin answer, as the deployer substitutes only a skill's declared
    variables and only in the files whose suffix it knows."""
    if Path(logical).suffix.casefold() not in SUFFIX_CONTEXT or not variables:
        return content
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return content

    def answer(match: re.Match[str]) -> str:
        return PLUGIN_VALUES[match.group(1)] if match.group(1) in variables else match.group(0)

    return TOKEN.sub(answer, text).encode("utf-8")


def leftover_tokens(files: Mapping[str, bytes]) -> list[str]:
    """Each line of a plugin's files that still holds a template token, as `path:line:text`."""
    found = []
    for path, content in sorted(files.items()):
        for number, line in enumerate(content.decode("utf-8", "replace").split("\n"), start=1):
            if TOKEN.search(line):
                found.append(f"{path}:{number}:{line}")
    return found


def namespaced(content: bytes, plugin: str, agents: list[str]) -> bytes:
    """A skill's SKILL.md with each backticked mention of an agent the plugin carries namespaced, as Claude Code names
    a plugin's agents."""
    for agent in agents:
        content = content.replace(f"`{agent}`".encode(), f"`{plugin}:{agent}`".encode())
    return content


def _agents(source: deploy_source.Source, plugin: Plugin, closure: list[str]) -> None:
    """Add each agent the closure needs, without its hooks, and the plugin's hooks file holding them."""
    hooks: dict[str, list[Node]] = {}
    for agent in deploy_source.agent_closure(source, closure):
        name = agent.removesuffix(".md")
        plugin.agents.append(name)
        text = render(agent, source.agents[name].read_bytes(), list(PLUGIN_VALUES)).decode("utf-8")
        try:
            definition, declared = plugin_agent(text)
        except AgentError as exc:
            raise BuildError(f"agent {name}: {exc}") from exc
        plugin.files[f"agents/{agent}"] = definition.encode("utf-8")
        for event, entries in declared.items():
            hooks.setdefault(event, []).extend(entries if isinstance(entries, list) else [entries])
    if hooks:
        plugin.files[HOOKS] = (json.dumps({"hooks": hooks}, indent=2) + "\n").encode("utf-8")


def _plugin(
    source: deploy_source.Source, name: str, closure: list[str], entry: dict[str, object], version: str
) -> Plugin:
    plugin = Plugin(name, skills=sorted(closure))
    _agents(source, plugin, closure)
    for skill in plugin.skills:
        variables = [variable for variable in source.skills[skill].required_vars if variable in PLUGIN_VALUES]
        for relative, content in shipped_files(source.skills[skill].directory).items():
            rendered = render(f"{skill}/{relative}", content, variables)
            if relative == "SKILL.md":
                rendered = namespaced(rendered, name, plugin.agents)
            plugin.files[f"skills/{skill}/{relative}"] = rendered
    manifest = {"name": name, "version": version, **{key: value for key, value in entry.items() if key != "source"}}
    plugin.files[MANIFEST] = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
    leftover = leftover_tokens(plugin.files)
    if leftover:
        raise BuildError(f"plugin {name} keeps template tokens: {'; '.join(leftover)}")
    return plugin


def assemble(
    source: deploy_source.Source, catalogue: dict[str, dict[str, object]], version: str = DEVELOPMENT_VERSION
) -> Assembly:
    """The plugins a source makes, in memory: the default selection's, then one per opt-in menu item, each described
    by its catalogue entry, which must name exactly the plugins made."""
    assembly = Assembly()
    default: list[str] = []
    opt_in: list[str] = []
    for item in [*sorted(source.bundles), *deploy_source.root_names(source)]:
        closure = deploy_source.expand(source, [], _members(source, item))
        reason = _blocker(source, closure)
        if reason is not None:
            assembly.left_out[item] = reason
        elif deploy_source.is_opt_in(source, item):
            opt_in.append(item)
        else:
            default.extend(name for name in closure if name not in default)
    name = plugin_name(source.source_id)
    closures = {name: default} if default else {}
    closures.update({f"{name}-{item}": deploy_source.expand(source, [], _members(source, item)) for item in opt_in})
    if set(closures) != set(catalogue):
        raise BuildError(
            f"the marketplace lists {', '.join(sorted(catalogue)) or 'no plugin'}, but the source makes "
            f"{', '.join(closures) or 'none'}"
        )
    for plugin, closure in closures.items():
        assembly.plugins.append(_plugin(source, plugin, closure, catalogue[plugin], version))
    return assembly


def archive_bytes(files: Mapping[str, bytes]) -> bytes:
    """A plugin's files as a zip whose bytes depend on the files alone."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        for path in sorted(files):
            entry = zipfile.ZipInfo(path, ARCHIVE_TIME)
            entry.external_attr = ARCHIVE_MODE
            entry.create_system = 3  # Unix, so the mode above is read as one on every platform
            archive.writestr(entry, files[path])
    return buffer.getvalue()


def output_directory(requested: Path | None, source_dir: Path) -> Path:
    """The folder the plugins are written into: a new one under the temporary directory, or the requested one, which
    must lie outside the source tree and every skills directory and be absent or empty."""
    if requested is None:
        return Path(tempfile.mkdtemp(prefix="coding-agent-skills-plugins-"))
    target = requested.resolve()
    if target.is_relative_to(source_dir.resolve()):
        raise BuildError(f"--output {requested} lies inside the source tree {source_dir}, which is never written")
    holder = skills_directory_holding(target)
    if holder is not None:
        raise BuildError(f"--output {requested} lies inside the skills directory {holder}, which a plugin never enters")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise BuildError(f"--output {requested} exists and is not an empty directory")
    target.mkdir(parents=True, exist_ok=True)
    return target


def write(assembly: Assembly, output: Path, *, archive: bool) -> list[str]:
    """Write every plugin into output, and return the lines that report it."""
    lines = [f'OUTPUT "{output}"']
    for plugin in assembly.plugins:
        if archive:
            content = archive_bytes(plugin.files)
            path = output / f"{plugin.name}.zip"
            path.write_bytes(content)
            lines.append(f'ARCHIVE {plugin.name} "{path}" {hashlib.sha256(content).hexdigest()}')
        else:
            root = output / plugin.name
            for relative, content in plugin.files.items():
                target = root.joinpath(*PurePosixPath(relative).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            lines.append(f'PLUGIN {plugin.name} "{root}"')
        lines.extend(f"SKILL {plugin.name} {skill}" for skill in plugin.skills)
        lines.extend(f"AGENT {plugin.name} {agent}" for agent in plugin.agents)
    lines.extend(f'LEFT_OUT {item} "{reason}"' for item, reason in assembly.left_out.items())
    return lines


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python tools/plugin_build.py", description="Assemble the Claude Code plugins from the skills."
    )
    parser.add_argument("--source", type=Path, default=REPOSITORY_ROOT, help="the source tree (default: this checkout)")
    parser.add_argument("--output", type=Path, help="an absent or empty folder outside the source tree to write into")
    parser.add_argument("--archive", action="store_true", help="write each plugin as a reproducible zip")
    parser.add_argument("--version", default=DEVELOPMENT_VERSION, help="the version each manifest carries")
    options = parser.parse_args(arguments)
    try:
        assembly = assemble(load_source(options.source), load_catalogue(options.source), options.version)
        lines = write(assembly, output_directory(options.output, options.source), archive=options.archive)
    except (BuildError, OSError) as exc:
        print(f"FAILED {flat_text(str(exc))}")
        return 1
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    run_main(main, errors="backslashreplace")
