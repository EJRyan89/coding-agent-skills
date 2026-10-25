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
with no plugin answer: a plugin carries nothing it cannot run as the deployer would. That plan, with PLUGIN_VALUES, is
ejryan89-skill-core's skill_menu, so audit-skill's script names the plugins that carry a skill the same way.

Each plugin is described by its entry in the source's marketplace file, .claude-plugin/marketplace.json, which names
every plugin the source makes and nothing else. Its manifest, .claude-plugin/plugin.json, is that entry without its
source, with the plugin's name and --version in place of any the entry gives (0.0.0-dev unless given; a release
passes its tag without the `v`), and, when a variable its skills take is answered by a plugin option, the userConfig
declaring that option. Claude Code substitutes an option only below a file's frontmatter, so the frontmatter names
it in words instead.
Claude Code namespaces a plugin's agents under its name, so a skill's backticked mention of an agent the plugin
carries names it as `<plugin>:<agent>`. A plugin agent's own hooks are ignored, so each agent's hooks move into the
plugin's hooks/hooks.json, as tools/plugin_agents.py describes. The source's .claude-plugin/README.md, when it has one,
is each plugin's README.md. A release builds from its tag with tools/plugin_release.py.

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

import frontmatter
import json_reader
from console import run_main
from flat_text import flat_text
from path_containment import within
from skill_menu import (
    PLUGIN_FRONTMATTER_VALUES,
    PLUGIN_HOOKS,
    PLUGIN_MANIFEST,
    PLUGIN_MARKETPLACE,
    PLUGIN_OPTIONS,
    PLUGIN_VALUES,
    plugin_plan,
)
from skill_roots import skills_directory_holding

from deployer import source as deploy_source
from deployer.errors import DeployError
from deployer.paths import Paths
from deployer.render import SUFFIX_CONTEXT, TOKEN, shipped_files
from tools.plugin_agents import AgentError, Node, plugin_agent

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
# The fixed time and mode of every archive entry, so an archive's bytes depend on its files alone.
ARCHIVE_TIME = (1980, 1, 1, 0, 0, 0)
ARCHIVE_MODE = 0o100644 << 16
MARKETPLACE = Path(PLUGIN_MARKETPLACE)
README = Path(".claude-plugin") / "README.md"
DEVELOPMENT_VERSION = "0.0.0-dev"
# A plugin option as Claude Code substitutes it in a skill or agent file.
USER_OPTION = re.compile(r"\$\{user_config\.([A-Za-z0-9_]+)\}")


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


def load_source(directory: Path) -> deploy_source.Source:
    paths = Paths(directory, directory)
    try:
        return deploy_source.discover(paths, deploy_source.load_source_id(paths))
    except DeployError as exc:
        raise BuildError(" ".join(exc.lines)) from exc


def load_catalogue(directory: Path) -> dict[str, dict[str, object]]:
    """The source's marketplace entries by plugin name. A source tree holds no plugin manifest of its own, which
    would make the skills in its checkout read as a plugin's."""
    if (directory / PLUGIN_MANIFEST).exists():
        raise BuildError(f"{directory / PLUGIN_MANIFEST} exists; a source tree is never a plugin itself")
    try:
        document = json_reader.read_file(directory / MARKETPLACE)
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


def render(logical: str, content: bytes, variables: list[str]) -> bytes:
    """A file with each variable it may take in its plugin answer, as the deployer substitutes only a skill's declared
    variables and only in the files whose suffix it knows. A Markdown file's frontmatter takes the words
    PLUGIN_FRONTMATTER_VALUES gives a plugin option, which Claude Code substitutes only below it."""
    suffix = Path(logical).suffix.casefold()
    if suffix not in SUFFIX_CONTEXT or not variables:
        return content
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return content

    def answered(part: str, values: Mapping[str, str]) -> str:
        return TOKEN.sub(lambda match: values[match.group(1)] if match.group(1) in variables else match.group(0), part)

    lines = text.split("\n")
    try:
        head = frontmatter.split(lines) if suffix == ".md" else None
    except frontmatter.FrontmatterError:
        head = None
    if head is None:
        return answered(text, PLUGIN_VALUES).encode("utf-8")
    end = sum(len(line) + 1 for line in lines[: head[1]])
    front = answered(text[:end], {**PLUGIN_VALUES, **PLUGIN_FRONTMATTER_VALUES})
    return (front + answered(text[end:], PLUGIN_VALUES)).encode("utf-8")


def plugin_options(source: deploy_source.Source, plugin: Plugin) -> dict[str, dict[str, object]]:
    """The userConfig a plugin's manifest declares: each plugin option the answer to a variable its skills declare or
    its agents hold reads, by name. Not read from the built files, whose scripts may name an option as text."""
    variables = {variable for skill in plugin.skills for variable in source.skills[skill].required_vars}
    for agent in plugin.agents:
        variables.update(TOKEN.findall(source.agents[agent].read_text(encoding="utf-8")))
    names = sorted(
        {
            name
            for variable in variables
            if variable in PLUGIN_VALUES
            for name in USER_OPTION.findall(PLUGIN_VALUES[variable])
        }
    )
    unknown = [name for name in names if name not in PLUGIN_OPTIONS]
    if unknown:
        raise BuildError(f"no plugin option is declared for {', '.join(unknown)}")
    return {name: PLUGIN_OPTIONS[name] for name in names}


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
        plugin.files[PLUGIN_HOOKS] = (json.dumps({"hooks": hooks}, indent=2) + "\n").encode("utf-8")


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
    kept = {key: value for key, value in entry.items() if key not in ("name", "version", "source")}
    manifest = {"name": name, "version": version, **kept}
    options = plugin_options(source, plugin)
    if options:
        manifest["userConfig"] = options
    plugin.files[PLUGIN_MANIFEST] = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
    leftover = leftover_tokens(plugin.files)
    if leftover:
        raise BuildError(f"plugin {name} keeps template tokens: {'; '.join(leftover)}")
    return plugin


def assemble(
    source: deploy_source.Source,
    catalogue: dict[str, dict[str, object]],
    version: str = DEVELOPMENT_VERSION,
    readme: bytes | None = None,
) -> Assembly:
    """The plugins a source makes, in memory, each described by its catalogue entry, which must name exactly the
    plugins made, and each carrying the README, if there is one."""
    closures, left_out = plugin_plan(source)
    if set(closures) != set(catalogue):
        raise BuildError(
            f"the marketplace lists {', '.join(sorted(catalogue)) or 'no plugin'}, but the source makes "
            f"{', '.join(closures) or 'none'}"
        )
    assembly = Assembly(left_out=left_out)
    for plugin, closure in closures.items():
        built = _plugin(source, plugin, closure, catalogue[plugin], version)
        if readme is not None:
            built.files["README.md"] = readme
        assembly.plugins.append(built)
    return assembly


def load_readme(directory: Path) -> bytes | None:
    """The README every plugin of the source tree carries, or None when the tree has none."""
    path = directory / README
    return path.read_bytes() if path.is_file() else None


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
    if within(target, source_dir.resolve()):
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
        source, catalogue = load_source(options.source), load_catalogue(options.source)
        assembly = assemble(source, catalogue, options.version, load_readme(options.source))
        lines = write(assembly, output_directory(options.output, options.source), archive=options.archive)
    except (BuildError, OSError) as exc:
        print(f"FAILED {flat_text(str(exc))}")
        return 1
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    run_main(main, errors="backslashreplace")
