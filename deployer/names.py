from __future__ import annotations

import re

from .errors import DeployError

RESERVED_WITH_EXTENSION = re.compile(r"^(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(\.|$)")
SAFE_NAME = re.compile(r"^[A-Za-z0-9._ -]+$")


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
    return None


def require_safe_name(name: str, context: str) -> None:
    problem = safe_name_problem(name, context)
    if problem is not None:
        raise DeployError(problem)
