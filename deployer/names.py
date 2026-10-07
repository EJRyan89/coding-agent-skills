from __future__ import annotations

import re

from . import platform_support
from .errors import DeployError

RESERVED_WITH_EXTENSION = re.compile(r"^(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(\.|$)")
SAFE_NAME = re.compile(r"^[A-Za-z0-9._ -]+$")
# The deployer's own files beside an item: <item>.deploying-bak is its transient backup during a deployment (plan.py,
# journal.py), and .<file>.tmp.<suffix> is an atomic write in progress (fsops.py). No item may take either form.
BACKUP_SUFFIX = ".deploying-bak"
TEMPORARY_INFIX = ".tmp."


def safe_name_problem(name: str, context: str) -> str | None:
    """Return why a name is not a safe single-level filename, or None when it is."""
    if not name:
        return f"ERROR: Empty {context}"
    if "/" in name or "\\" in name:
        return f"ERROR: {context} '{name}' contains path separator"
    if name in (".", ".."):
        return f"ERROR: {context} '{name}' is a dot segment"
    if name.startswith("."):
        return f"ERROR: {context} '{name}' starts with dot"
    if RESERVED_WITH_EXTENSION.match(name.upper()):
        return f"ERROR: {context} '{name}' is a Windows reserved name"
    if not SAFE_NAME.fullmatch(name):
        return f"ERROR: {context} '{name}' contains disallowed character"
    if name.endswith((".", " ")):
        return f"ERROR: {context} '{name}' has a trailing dot or space"
    return _reserved_problem(name, context)


def _reserved_problem(name: str, context: str) -> str | None:
    """Why a name takes a form the deployer reserves for its own files, compared as the file system compares names."""
    key = platform_support.name_key(name)
    reserved = "which the deployer reserves for its own files"
    if key.endswith(platform_support.name_key(BACKUP_SUFFIX)):
        return f"ERROR: {context} '{name}' ends in '{BACKUP_SUFFIX}', {reserved}"
    if platform_support.name_key(TEMPORARY_INFIX) in key:
        return f"ERROR: {context} '{name}' contains '{TEMPORARY_INFIX}', {reserved}"
    return None


def require_safe_name(name: str, context: str) -> None:
    problem = safe_name_problem(name, context)
    if problem is not None:
        raise DeployError(problem)
