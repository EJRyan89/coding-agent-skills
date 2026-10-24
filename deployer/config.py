"""Strict KEY=VALUE deployer configuration shared by configure and deploy."""

from __future__ import annotations

import os
import re
from pathlib import Path

from . import fsops, platform_support, state_files
from .arguments import CONFIGURE_COMMAND_LINE
from .errors import STATE_FILE, DeployError, see_recovery

ALLOW_PATH = "[A-Za-z0-9 /:.@_()-]"
# repo-cleanup's additions to its shipped lists: words separated by spaces, which the skill passes quoted to its
# script; the script, not the configuration, refuses an entry that could hold work.
CONFIGURED_VARIABLES: dict[str, str] = {
    "REPOS_ROOT": ALLOW_PATH,
    "UPDATE_CHANNEL": "[a-z]",
    "REPO_CLEANUP_REGENERABLE": "[A-Za-z0-9 ._*=,/-]",
    "REPO_CLEANUP_PROTECTED": "[A-Za-z0-9 ._/-]",
}
DIRECTORY_VARIABLES = ("REPOS_ROOT",)
# The configured variables whose value is one of a fixed set, which a saved configuration and configure's answer are
# both held to.
CHOICE_VARIABLES: dict[str, tuple[str, ...]] = {"UPDATE_CHANNEL": ("release", "main")}
# The configured variables an empty value answers, meaning none, rather than leaving unset.
OPTIONAL_VARIABLES = ("REPO_CLEANUP_REGENERABLE", "REPO_CLEANUP_PROTECTED")
# The value a configuration without the key takes, so a configuration saved before the key existed needs no change.
DEFAULTS: dict[str, str] = {"UPDATE_CHANNEL": "release", **dict.fromkeys(OPTIONAL_VARIABLES, "")}
PROMPTS: dict[str, str] = {
    "REPOS_ROOT": f"root directory for your git repositories, such as {platform_support.EXAMPLE_DIRECTORY}",
    "UPDATE_CHANNEL": "what update-coding-agent-skills deploys: release, the latest release, or main, the head of main",
    "REPO_CLEANUP_REGENERABLE": "ignored directories repo-cleanup may delete with a worktree, besides the ones it "
    "ships with, separated by spaces: a directory, or a directory, an equals sign, and the project files one of which "
    "must sit beside it, such as .venv .tox=tox.ini,setup.cfg; empty for none",
    "REPO_CLEANUP_PROTECTED": "branch prefixes repo-cleanup never deletes, besides the ones it ships with, separated "
    "by spaces, such as hotfix/ support/; empty for none",
}
# Rendering escapes a value only for the contexts it knows, so derived values pass an allowlist as configured ones
# do. Nobody chooses a home folder's name to suit the deployer, so letters of any script are allowed, but nothing a
# shell, PowerShell, YAML, or Markdown gives a meaning to.
ALLOW_DERIVED_PATH = r"[\w /:.@()-]"
DERIVED_VARIABLES: dict[str, str] = {
    "HOME": ALLOW_DERIVED_PATH,
    "SOURCE_ROOT": ALLOW_DERIVED_PATH,
    "PYTHON": "[a-z0-9]",
}
# The derived variables that name a program a command starts, rather than a path: each renders unquoted as a command
# word, so its value is one of a fixed set, never a configured one.
INTERPRETER_VARIABLES: dict[str, frozenset[str]] = {"PYTHON": frozenset({"python", "python3"})}
DERIVED_PROBLEM = "Derived paths may contain only letters, digits, spaces, and / : . @ _ ( ) -."
DERIVED_SOURCES: dict[str, tuple[str, str]] = {
    "HOME": ("the home folder", "Skills cannot be deployed into a home folder whose path has other characters."),
    "SOURCE_ROOT": (
        "the source checkout",
        "Move the checkout to a path without other characters, then deploy from there.",
    ),
    "PYTHON": ("the Python command", "This platform's Python command is not one the deployer renders."),
}
# A --canary-home deployment never reads the configuration; each configured directory is this folder of the
# throwaway home instead, so nothing a canary run does reaches the user's real directories.
CANARY_DIRECTORIES: dict[str, str] = {"REPOS_ROOT": "repos"}
SOURCE_KEY = "_source_id"
KEY_VALUE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def _disallowed(allowlist: str, value: str) -> tuple[int, str] | None:
    """The first character of `value` outside `allowlist`, with its position."""
    if re.fullmatch(f"{allowlist}*", value):
        return None
    return next((index, char) for index, char in enumerate(value) if not re.fullmatch(allowlist, char))


def _require_allowed(key: str, value: str) -> None:
    allowlist = CONFIGURED_VARIABLES.get(key)
    found = _disallowed(allowlist, value) if allowlist else None
    if found is not None:
        position, char = found
        raise DeployError(f"ERROR: Config key {key} contains disallowed character '{char}' at position {position}")
    choices = CHOICE_VARIABLES.get(key)
    if choices is not None and value not in choices:
        raise DeployError(f"ERROR: Config key {key} must be one of {', '.join(choices)} (got: {value})")


class UnknownKey(DeployError):
    """A saved key this deployer no longer declares, which `configure` drops and a deployment refuses."""


def parse(text: str, source_id: str, dropped: list[str] | None = None) -> dict[str, str]:
    """The configuration's values. Given a dropped list, a key no longer declared is added to it and skipped."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    values: dict[str, str] = {}
    for number, line in enumerate(lines, start=1):
        if "\r" in line:
            raise DeployError(f"ERROR: Config line {number} contains CRLF. Convert to LF.")
        if not line or line.startswith("#"):
            continue
        match = KEY_VALUE.fullmatch(line)
        if match is None:
            raise DeployError(f"ERROR: Config line {number}: malformed entry: {line}")
        key, value = match.groups()
        unknown = key != SOURCE_KEY and key not in CONFIGURED_VARIABLES
        if unknown and dropped is not None:
            # Its value is never used, so it is not checked: an obsolete key must not stop configure.
            dropped.append(key)
            continue
        if "$" in value or "`" in value:
            raise DeployError(f"ERROR: Config key {key} contains shell-active character ($ or backtick)")
        if unknown:
            raise UnknownKey(f"ERROR: Config key {key} is not a recognized variable")
        if key in values:
            raise DeployError(f"ERROR: Duplicate config key: {key}")
        _require_allowed(key, value)
        values[key] = value
    configured_source = values.get(SOURCE_KEY)
    if configured_source != source_id:
        shown = configured_source if configured_source is not None else "<missing>"
        raise DeployError(f"ERROR: Config _source_id ({shown}) does not match source.json ({source_id})")
    return values


def read(path: Path, source_id: str, dropped: list[str] | None = None) -> dict[str, str]:
    """Parse a saved configuration.

    `configure` reads it too, passing a dropped list, so it drops a key no longer declared; only `--reset` gets past
    any other failure.
    """
    remedy = f"Run '{CONFIGURE_COMMAND_LINE} --reset' to write a new configuration for this source."
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DeployError(f"ERROR: Config file is not valid UTF-8: {platform_support.normalize(path)}", remedy) from exc
    try:
        return parse(text, source_id, dropped)
    except UnknownKey as exc:
        raise DeployError(*exc.lines, f"Run '{CONFIGURE_COMMAND_LINE}' to drop it.") from exc
    except DeployError as exc:
        raise DeployError(*exc.lines, remedy) from exc


def _validate_absolute_directory(key: str, value: str) -> None:
    if not value:
        return
    if not platform_support.is_absolute(value):
        raise DeployError(f"ERROR: {key} must be {platform_support.ABSOLUTE_PATH} (got: {value})")
    if platform_support.is_filesystem_root(value):
        raise DeployError(f"ERROR: {key} must not be a filesystem root (got: {value})")
    if not Path(value).is_dir():
        raise DeployError(f"ERROR: {key} directory does not exist: {value}")
    try:
        canonical = platform_support.canonical_directory(value)
    except OSError as exc:
        raise DeployError(f"ERROR: {key} cannot be canonicalized: {value}") from exc
    if platform_support.is_filesystem_root(canonical):
        raise DeployError(f"ERROR: {key} resolves to filesystem root: {canonical}")


def validate_directories(values: dict[str, str]) -> None:
    for key in DIRECTORY_VARIABLES:
        _validate_absolute_directory(key, values.get(key, ""))


def is_saved(path: Path) -> bool:
    """Whether a configuration is saved at path, refusing a directory or anything else in its place."""
    return state_files.is_present(
        path, "The configuration", f"Move it aside, then run '{CONFIGURE_COMMAND_LINE}'.", see_recovery(STATE_FILE)
    )


def load(path: Path, source_id: str, home: Path, source_dir: Path) -> dict[str, str]:
    if not is_saved(path):
        raise DeployError(
            f"ERROR: No config found at {platform_support.normalize(path)}", f"Run '{CONFIGURE_COMMAND_LINE}' first."
        )
    values = {**DEFAULTS, **read(path, source_id)}
    values.update(derived_values(home, source_dir))
    try:
        validate_directories(values)
    except DeployError as exc:
        raise DeployError(*exc.lines, f"Run '{CONFIGURE_COMMAND_LINE}' to change it.") from exc
    return values


def canary(source_id: str, home: Path, source_dir: Path) -> dict[str, str]:
    """The values for a --canary-home deployment: every configured directory is a folder of that home, and every other
    configured variable takes its default."""
    values = {SOURCE_KEY: source_id, **DEFAULTS}
    for key, folder in CANARY_DIRECTORIES.items():
        values[key] = platform_support.normalize(home / folder)
        _require_allowed(key, values[key])
    values.update(derived_values(home, source_dir))
    for folder in CANARY_DIRECTORIES.values():
        fsops.make_directories(home / folder)
    validate_directories(values)
    return values


def derived_values(home: Path, source_dir: Path) -> dict[str, str]:
    """HOME, SOURCE_ROOT, and PYTHON for this run, refused when one has a character outside its allowlist or an
    interpreter is not one of its fixed set."""
    values = {
        # Lexical, as recorded in the manifest: Path.absolute keeps ".." and Path.resolve follows junctions.
        "HOME": platform_support.normalize(os.path.abspath(home)),  # noqa: PTH100 - lexical, as the comment says
        "SOURCE_ROOT": platform_support.normalize(os.path.abspath(source_dir)),  # noqa: PTH100 - lexical, as above
        "PYTHON": platform_support.PYTHON,
    }
    for key, names in INTERPRETER_VARIABLES.items():
        if values[key] not in names:
            description, remedy = DERIVED_SOURCES[key]
            raise DeployError(
                f"ERROR: {key} ({description}) is {values[key]!r}, not one of {', '.join(sorted(names))}", remedy
            )
    for key, value in values.items():
        found = _disallowed(DERIVED_VARIABLES[key], value)
        if found is not None:
            position, char = found
            description, remedy = DERIVED_SOURCES[key]
            raise DeployError(
                f"ERROR: {key} ({description}) contains disallowed character '{char}' at position {position}",
                DERIVED_PROBLEM,
                remedy,
            )
    return values
