"""Build a release's plugin archives from its tag, pin their checksums in the marketplace, and check the pins.

Usage:
  python tools/plugin_release.py build vX.Y.Z [--output DIR] [--root DIR]
  python tools/plugin_release.py check [--root DIR]

`build` exports the tag's tree from the checkout, as `deploy.py --at` does, so the archives hold what the tag holds
whatever the checkout has moved on to, and assembles each plugin from it with tools/plugin_build.py, its manifest
carrying the tag's version without the `v`. It writes each archive into a new folder under the system temporary
directory, or into --output, for the release to attach, and then pins each plugin's entry in the checkout's
.claude-plugin/marketplace.json to it: an `archive` source naming the release asset the tag's archive is published as,
under the entry's `repository`, with the archive's `sha256`, which Claude Code refuses to install on a mismatch. That
file is the one this tool writes in the checkout, for a pull request to carry. It prints:

  ARCHIVE <plugin> "<path>" <sha256>   an archive built from the tag, for the release to attach
  PINNED <plugin> <tag> <sha256>       the marketplace entry now names it

`check` rebuilds, from the tag each pinned entry's URL names, every archive the checkout's marketplace pins, and
compares the digests. It prints, for each plugin:

  PIN_MATCHES <plugin> <tag>                       the pinned digest is the rebuilt archive's
  PIN_DIFFERS <plugin> <tag> <pinned> <rebuilt>    it is not
  UNPINNED <plugin> "<reason>"                     the entry names no release asset with a digest

and exits 1 unless every plugin's pin matches. Either command prints `FAILED <reason>` and exits 1 when the tag is
not a release tag the checkout holds, its tree cannot be assembled, the marketplace lists no plugin, or a file cannot
be read or written; `build` refuses before it writes anything.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "ejryan89-skill-core" / "scripts"))

import json_reader
import json_writer
from console import run_main
from flat_text import flat_text
from release_tags import is_release_tag

from deployer import source_ref
from deployer.errors import DeployError
from tools import plugin_build
from tools.checkout import ROOT_HELP, checkout_root

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
# A release asset's URL: the repository's, its tag, and the archive's file name.
ASSET = re.compile(r"^(?P<repository>https://\S+?)/releases/download/(?P<tag>[^/]+)/(?P<file>[^/]+)\.zip$")


class ReleaseError(Exception):
    """A tag, tree, or marketplace a release cannot be built or checked from."""


def asset_url(repository: str, tag: str, plugin: str) -> str:
    return f"{repository.rstrip('/')}/releases/download/{tag}/{plugin}.zip"


def archives(root: Path, tag: str) -> dict[str, bytes]:
    """Each plugin's archive, built from the tag's tree with the tag's version, by plugin name."""
    if not is_release_tag(tag):
        raise ReleaseError(f"{tag} is not a release tag, vMAJOR.MINOR.PATCH")
    try:
        pin = source_ref.resolve(root, tag)
        export = source_ref.export(root, pin.commit)
    except DeployError as exc:
        raise ReleaseError(" ".join(exc.lines)) from exc
    try:
        tree = export / "tree"
        source, catalogue = plugin_build.load_source(tree), plugin_build.load_catalogue(tree)
        assembly = plugin_build.assemble(source, catalogue, tag[1:], plugin_build.load_readme(tree))
    except plugin_build.BuildError as exc:
        raise ReleaseError(f"the tree of {tag} cannot be assembled: {exc}") from exc
    finally:
        source_ref.remove(export)
    return {plugin.name: plugin_build.archive_bytes(plugin.files) for plugin in assembly.plugins}


def read_marketplace(root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The checkout's marketplace document, and its plugin entries, which must each be an object."""
    try:
        document = json_reader.read_file(root / plugin_build.MARKETPLACE)
    except (OSError, ValueError) as exc:
        raise ReleaseError(f"{plugin_build.MARKETPLACE.as_posix()} cannot be read: {exc}") from exc
    plugins = document.get("plugins") if isinstance(document, dict) else None
    if not isinstance(plugins, list) or not all(isinstance(entry, dict) for entry in plugins):
        raise ReleaseError(f"{plugin_build.MARKETPLACE.as_posix()} lists no plugins as objects")
    return document, plugins


def build(root: Path, tag: str, output: Path | None) -> list[str]:
    """Write the tag's archives into output and pin each marketplace entry to its release asset."""
    built = archives(root, tag)
    document, entries = read_marketplace(root)
    if {entry.get("name") for entry in entries} != set(built):
        raise ReleaseError(f"the marketplace does not list exactly the plugins {tag} makes: {', '.join(built)}")
    for entry in entries:
        repository = entry.get("repository")
        if not isinstance(repository, str) or not repository.startswith("https://"):
            raise ReleaseError(
                f"marketplace entry {entry['name']} names no https repository its release assets live under"
            )
    # The pins add only ASCII text, so a marketplace the writer takes now it takes once pinned: refused here, before
    # the first archive is written.
    try:
        json_writer.dumps(document)
    except json_writer.JsonWriteRefused as exc:
        raise ReleaseError(f"{plugin_build.MARKETPLACE.as_posix()} cannot be written: {exc}") from exc
    folder = plugin_build.output_directory(output, root)
    lines = []
    for entry in entries:
        name, repository = str(entry["name"]), str(entry["repository"])
        digest = hashlib.sha256(built[name]).hexdigest()
        path = folder / f"{name}.zip"
        path.write_bytes(built[name])
        entry["source"] = {"source": "archive", "url": asset_url(repository, tag, name), "sha256": digest}
        lines += [f'ARCHIVE {name} "{path}" {digest}', f"PINNED {name} {tag} {digest}"]
    json_writer.write(root / plugin_build.MARKETPLACE, document, indent=2, ensure_ascii=False, mode=0o644)
    return lines


def check(root: Path) -> tuple[list[str], bool]:
    """Each pinned plugin's verdict against a rebuild from its tag, and whether every pin matches."""
    lines: list[str] = []
    rebuilt: dict[str, dict[str, bytes]] = {}
    entries = read_marketplace(root)[1]
    if not entries:
        raise ReleaseError(f"{plugin_build.MARKETPLACE.as_posix()} lists no plugin, so there is no pin to check")
    for entry in entries:
        name = str(entry.get("name"))
        source = entry.get("source")
        url = source.get("url") if isinstance(source, dict) else None
        digest = source.get("sha256") if isinstance(source, dict) else None
        match = ASSET.fullmatch(url) if isinstance(url, str) else None
        if match is None or match.group("file") != name or not isinstance(digest, str):
            lines.append(f"UNPINNED {name} {json.dumps('its source names no release asset with a sha256')}")
            continue
        tag = match.group("tag")
        if tag not in rebuilt:
            rebuilt[tag] = archives(root, tag)
        if name not in rebuilt[tag]:
            lines.append(f"UNPINNED {name} {json.dumps(f'{tag} makes no plugin of this name')}")
            continue
        actual = hashlib.sha256(rebuilt[tag][name]).hexdigest()
        lines.append(f"PIN_MATCHES {name} {tag}" if actual == digest else f"PIN_DIFFERS {name} {tag} {digest} {actual}")
    return lines, all(line.startswith("PIN_MATCHES ") for line in lines)


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python tools/plugin_release.py", description="Build, pin, and check a release's plugin archives."
    )
    # --root follows the command, as the usage shows it.
    rooted = argparse.ArgumentParser(add_help=False)
    rooted.add_argument("--root", type=checkout_root, default=REPOSITORY_ROOT, metavar="DIR", help=ROOT_HELP)
    commands = parser.add_subparsers(dest="command", required=True)
    built = commands.add_parser(
        "build", parents=[rooted], help="build the tag's archives and pin them in the marketplace"
    )
    built.add_argument("tag", help="the release tag, vMAJOR.MINOR.PATCH")
    built.add_argument("--output", type=Path, help="an absent or empty folder outside the checkout to write into")
    commands.add_parser("check", parents=[rooted], help="compare each pinned digest with a rebuild from its tag")
    options = parser.parse_args(arguments)
    try:
        if options.command == "build":
            print("\n".join(build(options.root, options.tag, options.output)))
            return 0
        lines, matched = check(options.root)
    except (ReleaseError, plugin_build.BuildError, OSError, json_writer.JsonWriteRefused) as exc:
        print(f"FAILED {flat_text(str(exc))}")
        return 1
    print("\n".join(lines))
    return 0 if matched else 1


if __name__ == "__main__":
    run_main(main, errors="backslashreplace")
