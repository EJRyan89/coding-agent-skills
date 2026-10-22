"""What repo-cleanup may delete with a worktree and which branches it never deletes: the shipped lists in
`references/cleanup-policy.json`, and the additions the user configured.

The file is one JSON object, read strictly and never executed:

    {"schema_version": 1,
     "protected_prefixes": ["<prefix>", ...],
     "regenerable": [{"directory": "<directory>", "projects": ["<project file pattern>", ...]}, ...]}

A regenerable directory is one a tool rebuilds from what the worktree holds, named by its path from the folder that
holds it, with the files one of which must sit beside it for it to count: the project the build or the package
manager rebuilds it from, or none. An ignored path inside one carries no work of the user's, so a worktree holding
only such paths is removed with them; any other ignored path, such as a `.env` or a `bin` with no project beside it,
preserves the worktree. A protected prefix is one a branch name, or a folder of a worktree's path, starts with, in any
letter case: such a branch is never deleted and such a worktree never removed.

The user adds to both lists through the deployer's configured variables, which the skill passes to `sweep`: the
regenerable additions as entries separated by spaces, each a directory or a directory, an equals sign, and its
project files separated by commas (`.venv .tox=tox.ini,setup.cfg`), and the protected additions as prefixes separated
by spaces. An addition never removes or changes a shipped entry. An added regenerable entry that could hold work is
refused, with its reason, and not used, so every worktree holding paths only it would have covered is kept;
`refusal` names what it refuses. The shipped entries are held to the same rules, so a file that breaks one stops the
skill.
"""

from __future__ import annotations

import functools
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from diff_paths import DRIVE
from typed_fields import FieldError, FieldReader

POLICY_FILE = Path(__file__).resolve().parents[1] / "references" / "cleanup-policy.json"
SCHEMA_VERSION = 1
# What a directory name may not hold: it is matched by its name as written, never as a pattern.
WILDCARDS = re.compile(r"[*?\[\]]")
BACKSLASH = chr(92)


class PolicyError(ValueError):
    """A shipped policy file that cannot be read or is not in the shape the module docstring gives."""


class PolicyFieldError(FieldError, PolicyError):
    """A field of the shipped policy file that is not of the kind the shape gives."""


READ = FieldReader(PolicyFieldError)


@dataclass(frozen=True)
class Regenerable:
    """A regenerable directory, by its path's folders, with the project file patterns one of which must sit beside
    it, or none; `added` when the user's configuration, not the shipped file, names it."""

    directory: tuple[str, ...]
    projects: tuple[str, ...]
    added: bool = False

    def written(self) -> str:
        """The entry as the configuration writes it."""
        name = "/".join(self.directory)
        return f"{name}={','.join(self.projects)}" if self.projects else name


@dataclass(frozen=True)
class Refused:
    """A configured entry that is not used, as written, and why."""

    entry: str
    reason: str


@dataclass(frozen=True)
class Policy:
    """The lists a sweep uses: the shipped ones with the accepted additions, and each refused addition.

    `given` holds the configured values as they were given, regenerable then protected, so a plan records them and
    every later command rebuilds the same policy from them.
    """

    regenerable: tuple[Regenerable, ...]
    protected: tuple[str, ...]
    added_protected: tuple[str, ...]
    refused: tuple[Refused, ...]
    given: tuple[str, str]

    def added_regenerable(self) -> list[str]:
        """The accepted regenerable additions, as written."""
        return [entry.written() for entry in self.regenerable if entry.added]

    def protecting(self, name: str) -> str | None:
        """The protected prefix `name` starts with, in any letter case, or None.

        Matched in any letter case: Windows treats Release and release as one directory, and refs stored as files
        there cannot hold both, so protection errs toward keeping.
        """
        folded = name.casefold()
        return next((prefix for prefix in self.protected if folded.startswith(prefix.casefold())), None)


def refusal(directory: str, projects: list[str], shipped_names: set[tuple[str, ...]]) -> str:
    """Why a regenerable entry could hold work, or an empty string when it may be used.

    A directory must be a relative path inside the worktree with no empty, `.`, or `..` folder and no wildcard; an
    addition may not name a shipped directory, which would change the projects it needs. Each project is a file name
    pattern for a file beside the directory, never a path.
    """
    name = directory.replace(BACKSLASH, "/")
    folders = name.split("/")
    if not name:
        return "names no directory"
    if name.startswith("/") or DRIVE.match(name):
        return "is an absolute path; name a directory inside the worktree"
    if ".." in folders:
        return "is a parent-relative path, which reaches outside the worktree"
    if all(folder in ("", ".") for folder in folders):
        return "names the whole worktree"
    if any(folder in ("", ".") for folder in folders):
        return "holds an empty or '.' folder; write the path from the folder that holds the directory"
    if WILDCARDS.search(name):
        return "holds a wildcard; a directory is matched by its name as written"
    if tuple(folders) in shipped_names:
        return "is a shipped entry; an addition cannot change the project files it needs"
    for project in projects:
        if not project:
            return "names an empty project file"
        if project in (".", "..") or "**" in project or any(character in project for character in "/\\:="):
            return f"names project file {project}, which is not a file name beside the directory"
    return ""


def shipped(path: Path | None = None) -> tuple[tuple[Regenerable, ...], tuple[str, ...]]:
    """The shipped regenerable entries and protected prefixes, read from `path`, POLICY_FILE when None; PolicyError
    when the file cannot be read, is not in the documented shape, or holds an entry that could hold work."""
    return _read(path or POLICY_FILE)


@functools.cache
def _read(path: Path) -> tuple[tuple[Regenerable, ...], tuple[str, ...]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PolicyError(f"cannot read the cleanup policy {path.as_posix()}: {exc}") from exc
    fields = READ.json_object(document, "$", required={"schema_version", "protected_prefixes", "regenerable"})
    READ.one_of(fields["schema_version"], "$.schema_version", (SCHEMA_VERSION,))
    prefixes = READ.array(fields["protected_prefixes"], "$.protected_prefixes", _prefix, nonempty=True)
    entries = READ.array(fields["regenerable"], "$.regenerable", _entry, nonempty=True)
    for index, (directory, projects) in enumerate(entries):
        reason = refusal(directory, projects, set())
        if reason:
            raise PolicyError(f"{path.as_posix()}: $.regenerable[{index}] {directory} {reason}")
    names = [tuple(directory.replace(BACKSLASH, "/").split("/")) for directory, _ in entries]
    if len(set(names)) != len(names):
        raise PolicyError(f"{path.as_posix()}: $.regenerable names a directory more than once")
    return (
        tuple(Regenerable(name, tuple(projects)) for name, (_, projects) in zip(names, entries, strict=True)),
        tuple(dict.fromkeys(prefixes)),
    )


def _prefix(value: Any, path: str) -> str:
    return READ.string(value, path, single_line=True, trimmed=True)


def _entry(value: Any, path: str) -> tuple[str, list[str]]:
    fields = READ.json_object(value, path, required={"directory", "projects"})
    directory = READ.string(fields["directory"], f"{path}.directory", single_line=True, trimmed=True)
    projects = READ.array(fields["projects"], f"{path}.projects", _prefix)
    return directory, projects


def configured(regenerable: str = "", protected: str = "", path: Path | None = None) -> Policy:
    """The shipped lists from `path`, POLICY_FILE when None, with the configured additions: `regenerable` entries and
    `protected` prefixes, each separated by whitespace. A refused regenerable entry is named in `refused` and left
    out."""
    defaults, prefixes = shipped(path)
    shipped_names = {entry.directory for entry in defaults}
    added: list[Regenerable] = []
    refused: list[Refused] = []
    for written in dict.fromkeys(regenerable.split()):
        directory, equals, listed = written.partition("=")
        projects = listed.split(",") if equals else []
        reason = refusal(directory, projects, shipped_names)
        if reason:
            refused.append(Refused(written, reason))
            continue
        entry = Regenerable(tuple(directory.replace(BACKSLASH, "/").split("/")), tuple(projects), added=True)
        if entry not in added:
            added.append(entry)
    known = {prefix.casefold() for prefix in prefixes}
    added_prefixes = [prefix for prefix in dict.fromkeys(protected.split()) if prefix.casefold() not in known]
    return Policy(
        regenerable=(*defaults, *added),
        protected=(*prefixes, *added_prefixes),
        added_protected=tuple(added_prefixes),
        refused=tuple(refused),
        given=(regenerable, protected),
    )
