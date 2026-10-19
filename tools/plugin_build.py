"""Assemble the Claude Code plugins from the skills, their agents, and their metadata, which stay the one source.

Usage:
  python tools/plugin_build.py [--source DIR] [--output DIR] [--archive]

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
with no plugin answer, or an agent it needs declares hooks, which a plugin agent ignores: a plugin carries nothing it
cannot run as the deployer would.

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
import re
import sys
import tempfile
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "ejryan89-skill-core" / "scripts"))

import frontmatter
from console import run_main
from flat_text import flat_text
from skill_roots import skills_directory_holding

from deployer import source as deploy_source
from deployer.errors import DeployError
from deployer.paths import Paths
from deployer.render import SUFFIX_CONTEXT, TOKEN, shipped_files

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
# What each template value becomes in a plugin, which has no deployer to render it. A plugin is one tree for every
# platform, so the interpreter is the command the deployer renders on Windows, and the plugin's README states that it
# must start Python. A value missing here has no plugin answer, and a skill that takes one stays out of every plugin.
PLUGIN_VALUES: dict[str, str] = {"PYTHON": "python"}
# The fixed time and mode of every archive entry, so an archive's bytes depend on its files alone.
ARCHIVE_TIME = (1980, 1, 1, 0, 0, 0)
ARCHIVE_MODE = 0o100644 << 16
NO_VALUE = "{skill} takes {{{{{variable}}}}}, which a plugin has no value for"
AGENT_HOOKS = "agent {agent} declares hooks, which a plugin agent ignores"


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


def _blocker(source: deploy_source.Source, closure: list[str]) -> str | None:
    """Why a plugin cannot carry a menu item with this closure, or None when it can."""
    for name in closure:
        for variable in source.skills[name].required_vars:
            if variable not in PLUGIN_VALUES:
                return NO_VALUE.format(skill=name, variable=variable)
    for agent in deploy_source.agent_closure(source, closure):
        name = agent.removesuffix(".md")
        if "hooks" in frontmatter.read(source.agents[name]):
            return AGENT_HOOKS.format(agent=name)
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


def _plugin(source: deploy_source.Source, name: str, closure: list[str]) -> Plugin:
    plugin = Plugin(name, skills=sorted(closure))
    for skill in plugin.skills:
        variables = [variable for variable in source.skills[skill].required_vars if variable in PLUGIN_VALUES]
        for relative, content in shipped_files(source.skills[skill].directory).items():
            plugin.files[f"skills/{skill}/{relative}"] = render(f"{skill}/{relative}", content, variables)
    for agent in deploy_source.agent_closure(source, closure):
        plugin.agents.append(agent.removesuffix(".md"))
        content = source.agents[agent.removesuffix(".md")].read_bytes()
        plugin.files[f"agents/{agent}"] = render(agent, content, list(PLUGIN_VALUES))
    leftover = leftover_tokens(plugin.files)
    if leftover:
        raise BuildError(f"plugin {name} keeps template tokens: {'; '.join(leftover)}")
    return plugin


def assemble(source: deploy_source.Source) -> Assembly:
    """The plugins a source makes, in memory: the default selection's, then one per opt-in menu item."""
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
    if default:
        assembly.plugins.append(_plugin(source, name, default))
    for item in opt_in:
        closure = deploy_source.expand(source, [], _members(source, item))
        assembly.plugins.append(_plugin(source, f"{name}-{item}", closure))
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
    options = parser.parse_args(arguments)
    try:
        assembly = assemble(load_source(options.source))
        lines = write(assembly, output_directory(options.output, options.source), archive=options.archive)
    except (BuildError, OSError) as exc:
        print(f"FAILED {flat_text(str(exc))}")
        return 1
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    run_main(main, errors="backslashreplace")
