"""The one reading of what ends a Python program with a code: the calls that exit, the keyword each takes its code by,
and the code an argument gives whatever the program holds.

Validation's exit tables (`tests/validation/exit_tables.py`) and its script-results policy
(`tests/validation/skill_scripts.py`) read a script's exits by it, and so does audit-skill's inventory
(`skills/audit-skill/scripts/skill_inventory.py`), so a call or a rule added here reaches all three.
"""

from __future__ import annotations

import ast

# What ends the process with a code, by the dotted name a module's imports resolve it to: sys.exit, the exit and quit
# builtins, os._exit, and SystemExit, called or raised.
SYSTEM_EXIT = "SystemExit"
EXIT_CALLS = frozenset({"sys.exit", "exit", "quit", "builtins.exit", "builtins.quit", "os._exit", SYSTEM_EXIT})
# The same calls by the last part of their names, for a reader that does not resolve a module's imports.
CALL_NAMES = frozenset(name.rpartition(".")[2] for name in EXIT_CALLS)
# What an exit given no code, or None, exits with, as a SystemExit raised as the class does, and what one given a
# message, which Python prints, exits with.
NO_CODE = 0
MESSAGE_CODE = 1


def code_keyword(function: str) -> str:
    """The keyword an exit call, named either way, takes its code by: os._exit's status, and every other's code."""
    return "status" if function.rpartition(".")[2] == "_exit" else "code"


def fixed_code(argument: ast.expr | None) -> int | None:
    """The code an exit call's argument exits with whatever the program holds: none, or None, exits NO_CODE, and a
    string exits MESSAGE_CODE. Any other argument gives None, for the caller to read as the expression it is."""
    if argument is None or (isinstance(argument, ast.Constant) and argument.value is None):
        return NO_CODE
    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
        return MESSAGE_CODE
    return None
