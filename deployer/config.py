"""Strict KEY=VALUE deployer configuration shared by configure and deploy."""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import quote

from . import fsops, platform_support
from .arguments import CONFIGURE_COMMAND_LINE
from .errors import DeployError

ALLOW_PATH = "[A-Za-z0-9 /:.@_()-]"
CONFIGURED_VARIABLES: dict[str, str] = {"REPOS_ROOT": ALLOW_PATH}
DIRECTORY_VARIABLES = ("REPOS_ROOT",)
PROMPTS: dict[str, str] = {"REPOS_ROOT": "root directory for your git repositories, such as C:/GitHub"}
DERIVED_VARIABLES = ("HOME", "HOME_URI", "SOURCE_ROOT")
# A --canary-home deployment never reads the configuration; each configured directory is this folder of the
# throwaway home instead, so nothing a canary run does reaches the user's real directories.
CANARY_DIRECTORIES: dict[str, str] = {"REPOS_ROOT": "repos"}
SOURCE_KEY = "_source_id"
KEY_VALUE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def _require_allowed(key: str, value: str) -> None:
    allowlist = CONFIGURED_VARIABLES.get(key)
    if allowlist and value and not re.fullmatch(f"{allowlist}+", value):
        position, char = next(
            (index, c) for index, c in enumerate(value) if not re.fullmatch(allowlist, c)
        )
        raise DeployError(
            f"ERROR: Config key {key} contains disallowed character '{char}' at position {position}"
        )


def parse(text: str, source_id: str) -> dict[str, str]:
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
        if "$" in value or "`" in value:
            raise DeployError(
                f"ERROR: Config key {key} contains shell-active character ($ or backtick)"
            )
        if key != SOURCE_KEY and key not in CONFIGURED_VARIABLES:
            raise DeployError(f"ERROR: Config key {key} is not a recognized variable")
        if key in values:
            raise DeployError(f"ERROR: Duplicate config key: {key}")
        _require_allowed(key, value)
        values[key] = value
    configured_source = values.get(SOURCE_KEY)
    if configured_source != source_id:
        shown = configured_source if configured_source is not None else "<missing>"
        raise DeployError(
            f"ERROR: Config _source_id ({shown}) does not match source.json ({source_id})"
        )
    return values


def read(path: Path, source_id: str) -> dict[str, str]:
    """Parse a saved configuration. `configure` reads it too, so only `--reset` gets past one that fails."""
    remedy = f"Run '{CONFIGURE_COMMAND_LINE} --reset' to write a new configuration for this source."
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DeployError(f"ERROR: Config file is not valid UTF-8: {path}", remedy) from exc
    try:
        return parse(text, source_id)
    except DeployError as exc:
        raise DeployError(*exc.lines, remedy) from exc


def _validate_absolute_directory(key: str, value: str) -> None:
    if not value:
        return
    if not platform_support.is_absolute(value):
        raise DeployError(f"ERROR: {key} must be an absolute Windows path (got: {value})")
    if platform_support.is_filesystem_root(value):
        raise DeployError(f"ERROR: {key} must not be a filesystem root (got: {value})")
    if not os.path.isdir(value):
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


def load(path: Path, source_id: str, home: Path, source_dir: Path) -> dict[str, str]:
    if not path.is_file():
        raise DeployError(f"ERROR: No config found at {path}", f"Run '{CONFIGURE_COMMAND_LINE}' first.")
    values = read(path, source_id)
    try:
        return _derive(values, home, source_dir)
    except DeployError as exc:
        raise DeployError(*exc.lines, f"Run '{CONFIGURE_COMMAND_LINE}' to change it.") from exc


def canary(source_id: str, home: Path, source_dir: Path) -> dict[str, str]:
    """The values for a --canary-home deployment: every configured directory is a folder of that home."""
    values = {SOURCE_KEY: source_id}
    for key, folder in CANARY_DIRECTORIES.items():
        values[key] = platform_support.normalize(home / folder)
        _require_allowed(key, values[key])
    for key, folder in CANARY_DIRECTORIES.items():
        fsops.make_directories(home / folder)
    return _derive(values, home, source_dir)


def _derive(values: dict[str, str], home: Path, source_dir: Path) -> dict[str, str]:
    home_value = platform_support.normalize(os.path.abspath(home))
    values["HOME"] = home_value
    values["HOME_URI"] = quote(home_value, safe="/:")
    values["SOURCE_ROOT"] = platform_support.normalize(os.path.abspath(source_dir))
    validate_directories(values)
    return values
