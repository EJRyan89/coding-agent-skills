"""Where skills live: a sibling skill's scripts, and the skills directories a script must not write inside.

A script reaches a skill_deps sibling's modules through `sibling_scripts`, after the one line that puts
ejryan89-skill-core on sys.path, so the layout is known here once, in the checkout, the deployed home, and a Claude Code
plugin alike: each holds its skills in one flat directory. `plugin_name` tells a plugin apart, for what a plugin names
otherwise, such as its agents, which Claude Code namespaces under the plugin's name.

A file left inside a skill directory makes the deployer see that skill as modified and stop updating it, so a
script refuses an explicit output path inside its own skills directory or one the deployer owns.
"""

from __future__ import annotations

import json
from pathlib import Path

from console import failed_line, use_utf8_output
from file_presence import is_present

# This skill's own folder name, which every dependent's fixed sys.path line and every check of that line names.
SKILL_CORE = "ejryan89-skill-core"
# The skills directory holding ejryan89-skill-core, and so every skill that imports it: the source tree's skills/, the
# deployed ~/.claude/skills, or a plugin's skills/.
SKILLS_ROOT = Path(__file__).resolve().parents[2]
# A Claude Code plugin's manifest, beside the plugin's skills directory; neither the checkout nor the deployed home has
# one there.
PLUGIN_MANIFEST = Path(".claude-plugin") / "plugin.json"
# The prefixes of Windows' extended-length spellings: `\\?\C:\x` names `C:\x`, and `\\?\UNC\host\share\x` names
# `\\host\share\x`. `Path.resolve` keeps either, and gives a volume's GUID path or a `\\.\` path back with the first,
# so a path and a root are compared without them. pathlib reads each as part of a path's drive, which a path has only
# on Windows, so elsewhere the same characters are an ordinary name and are kept.
EXTENDED_PREFIX = "\\\\?\\"
EXTENDED_UNC_PREFIX = "\\\\?\\UNC\\"


class MissingSibling(SystemExit):
    """A sibling skill a script reaches is not beside it.

    `sibling_scripts` runs while a script imports, before `console.run_main` can turn an exception into a FAILED
    line, so the lookup prints the line `run_main` would, `FAILED MissingSibling: <message>` on stdout, and this exit
    then ends the script with exit code 1 and nothing on stderr, rather than with a traceback.
    """

    def __init__(self, name: str, message: str) -> None:
        super().__init__(1)
        self.name = name
        self.message = message

    def __str__(self) -> str:
        return self.message


def sibling_scripts(name: str) -> Path:
    """The scripts directory of the sibling skill `name`, for a script to put on sys.path.

    The skills directory is flat in the checkout, the deployed home, and a plugin alike, so the sibling is
    SKILLS_ROOT/<name>, beside ejryan89-skill-core.
    """
    scripts = SKILLS_ROOT / name / "scripts"
    if scripts.is_dir():
        return scripts
    try:
        plugin = plugin_name()
    except ValueError as error:
        raise _reported(
            name, f"skill {name} has no scripts under {SKILLS_ROOT}; reinstall the plugin: {error}"
        ) from None
    remedy = (
        f"update or reinstall the plugin {plugin}"
        if plugin is not None
        else "deploy the skills that declare it in skill_deps again"
    )
    raise _reported(name, f"skill {name} has no scripts under {SKILLS_ROOT}; {remedy}")


def _reported(name: str, message: str) -> MissingSibling:
    """The exit for an absent sibling, once its FAILED line is out on stdout in UTF-8, as `run_main` prints one."""
    missing = MissingSibling(name, message)
    use_utf8_output()
    print(failed_line(missing), flush=True)
    return missing


def plugin_name() -> str | None:
    """The name of the Claude Code plugin these skills run from, or None in the checkout and the deployed home.

    A plugin holds its manifest beside its skills directory, and Claude Code namespaces every component the plugin
    carries under the manifest's name, so an agent `reviewer` of plugin `suite` is `suite:reviewer`. A manifest that
    cannot be read, such as a directory in its place, is not JSON, or names no plugin raises ValueError: the skills run
    from a plugin whose name cannot be known.
    """
    manifest = SKILLS_ROOT.parent / PLUGIN_MANIFEST
    try:
        if not is_present(manifest):
            return None
        document: object = json.loads(manifest.read_text(encoding="utf-8"))
    except OSError as error:
        raise ValueError(f"{manifest} cannot be read: {error.strerror or error}") from error
    except ValueError as error:
        raise ValueError(f"{manifest} is not JSON: {error}") from error
    name = document.get("name") if isinstance(document, dict) else None
    if not isinstance(name, str) or not name:
        raise ValueError(f"{manifest} names no plugin")
    return name


def deployed_skill_roots() -> tuple[Path, ...]:
    """The directories the deployer owns, whatever skills directory this script runs from."""
    home = Path.home()
    return home / ".claude" / "skills", home / ".agents" / "skills"


def _without_extended_prefix(path: Path) -> Path:
    """`path` without Windows' extended-length prefix, when one comes before a drive letter or a UNC share; any other
    path, such as a volume's GUID path that no drive letter names, is kept as it is."""
    if not path.drive:
        return path
    text = str(path)
    if text[: len(EXTENDED_UNC_PREFIX)].upper() == EXTENDED_UNC_PREFIX:
        plain = Path("\\\\" + text[len(EXTENDED_UNC_PREFIX) :])
    elif text.startswith(EXTENDED_PREFIX):
        plain = Path(text[len(EXTENDED_PREFIX) :])
    else:
        return path
    return plain if plain.drive else path


def skills_directory_holding(path: Path) -> Path | None:
    """The skills directory `path` lies inside, the running skills' own or one the deployer owns, or None. Each is
    compared resolved and without an extended-length prefix, so every spelling of a path inside one is found."""
    target = _without_extended_prefix(path.resolve())
    return next(
        (
            root
            for root in (SKILLS_ROOT, *deployed_skill_roots())
            if target.is_relative_to(_without_extended_prefix(root.resolve()))
        ),
        None,
    )
