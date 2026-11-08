from __future__ import annotations

import re

from . import platform_support
from .errors import DeployError

SAFE_NAME = re.compile(r"^[A-Za-z0-9._ -]+$")
# The deployer's own files beside an item: <item>.deploying-bak is its transient backup during a deployment (plan.py,
# journal.py), .<item>.deploying-del a copy of it being deleted (journal.py), or .<item>.deploying-del.<token> while
# something an earlier deletion left holds the first name, and .<file>.tmp.<suffix> an atomic write in progress
# (fsops.py). No item may take any of these forms; a safe name never starts with a dot.
BACKUP_SUFFIX = ".deploying-bak"
DELETION_SUFFIX = ".deploying-del"
# The token that tells one spare deletion name of an item from another: lowercase hex of a fixed length, which no
# deletion name of another item can end in, since every one ends in DELETION_SUFFIX or in another such token.
SPARE_DELETION_TOKEN = re.compile(r"[0-9a-f]{8}")
TEMPORARY_INFIX = ".tmp."


def transient_name(item: str) -> str:
    """The name of item's transient backup, beside it while a deployment replaces it."""
    return f"{item}{BACKUP_SUFFIX}"


def deletion_name(item: str) -> str:
    """The name a copy of item takes while the deployer deletes it, so a deletion stopped partway leaves what is left
    of the copy under a name only the deployer uses, never at a name whose content recovery judges by its hash."""
    return f".{item}{DELETION_SUFFIX}"


def spare_deletion_name(item: str, token: str) -> str:
    """The name a copy of item takes while the deployer deletes it and what an earlier deletion left at
    deletion_name(item) cannot be deleted: deletion_name(item) and a token SPARE_DELETION_TOKEN matches, so a held
    leftover never makes the deployer delete a copy where it stands."""
    if not SPARE_DELETION_TOKEN.fullmatch(token):
        raise ValueError(f"Not a spare deletion token: {token!r}")
    return f"{deletion_name(item)}.{token}"


def spare_deletion_token(name_key: str, deletion_key: str) -> str | None:
    """The token of a spare deletion name, both names in the form the file system compares them, or None when name is
    not a spare deletion name of the item whose deletion name has deletion_key."""
    prefix = f"{deletion_key}."
    if not name_key.startswith(prefix):
        return None
    token = name_key[len(prefix) :]
    return token if SPARE_DELETION_TOKEN.fullmatch(token) else None


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
    if platform_support.is_reserved_device_name(name):
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
