"""The one reading of what ends a Python program with a code: the calls that exit, the keyword each takes its code by,
and the code an argument gives whatever the program holds. A reader compares a call with EXIT_CALLS by the dotted name
the module's imports resolve it to (ejryan89-skill-core's python_names), never by its last part, which a method such
as `smtp.quit()` shares. A call's code is read here as Python reads the call: call_code gives each code its arguments
fix, a call Python refuses for its arguments included, and exit_argument the one expression it exits with otherwise.

Validation's exit tables (`tests/validation/exit_tables.py`) and its script-results policy
(`tests/validation/skill_scripts.py`) read a script's exits by it, and so does audit-skill's inventory
(`skills/audit-skill/scripts/skill_inventory.py`), so a call or a rule added here reaches all three.
"""

from __future__ import annotations

import ast

# What ends the process with a code, by the dotted name a module's imports resolve it to: sys.exit, the exit and quit
# builtins, os._exit, and SystemExit, called or raised. tests/validation/duplication.py fails a copy of them.
EXIT_CALLS = frozenset({"sys.exit", "exit", "quit", "builtins.exit", "builtins.quit", "os._exit", "SystemExit"})
SYSTEM_EXIT = "SystemExit"
# The exit calls that take their code by position alone: sys.exit's one parameter is positional-only, and SystemExit
# takes no keyword argument, so Python refuses either given a keyword.
POSITIONAL_ONLY = frozenset({"sys.exit", "SystemExit"})
# What an exit given no code, or None, exits with, as a SystemExit raised as the class does, and what one given a
# message, which Python prints, exits with.
NO_CODE = 0
MESSAGE_CODE = 1
# What an exit call Python refuses for its arguments exits with: the TypeError it raises, uncaught, exits 1.
REFUSED_CALL_CODE = 1


def code_keyword(function: str) -> str | None:
    """The keyword an exit call, by its dotted name, takes its code by: os._exit's status, and the other calls' code,
    but None for sys.exit and SystemExit, which take it by position alone."""
    if function in POSITIONAL_ONLY:
        return None
    return "status" if function == "os._exit" else "code"


def _refused(call: ast.Call, function: str) -> bool:
    """Whether Python refuses the call's written arguments with a TypeError: a keyword the call does not take, or the
    code given by position and by its keyword at once. More than one positional argument exits 1 too, as a TypeError
    for every call but SystemExit, which prints the tuple of them as its message, so call_code reads it as refused.
    A stated limit: `os._exit()` with no status, and `os._exit(None)`, are read as exiting 0, as every other exit
    given no code is, though Python refuses both with a TypeError, which exits 1."""
    keyword = code_keyword(function)
    named = [given.arg for given in call.keywords if given.arg is not None]
    positional = [given for given in call.args if not isinstance(given, ast.Starred)]
    return any(name != keyword for name in named) or (keyword in named and bool(positional)) or len(positional) > 1


def exit_argument(call: ast.Call, function: str) -> ast.expr | None:
    """The argument an exit call, `function` by its dotted name, takes its code from, by position or by its keyword, or
    None when it gives none. A `*` or `**` unpacking that may hold it comes back as an ast.Starred around what it
    unpacks, which reads as no literal."""
    if call.args:
        return call.args[0]
    keyword = code_keyword(function)
    for given in call.keywords:
        if given.arg is None:
            return ast.copy_location(ast.Starred(value=given.value, ctx=ast.Load()), given.value)
        if given.arg == keyword:
            return given.value
    return None


def call_code(call: ast.Call, function: str) -> int | None:
    """The code an exit call exits with whatever the program holds: REFUSED_CALL_CODE for a call Python refuses for its
    arguments, and otherwise what fixed_code reads its exit_argument as. None leaves the argument to the caller, to
    read as the expression it is."""
    if _refused(call, function):
        return REFUSED_CALL_CODE
    return fixed_code(exit_argument(call, function))


def fixed_code(argument: ast.expr | None) -> int | None:
    """The code an exit call's argument exits with whatever the program holds: none, or None, exits NO_CODE, and a
    string exits MESSAGE_CODE. Any other argument gives None, for the caller to read as the expression it is."""
    if argument is None or (isinstance(argument, ast.Constant) and argument.value is None):
        return NO_CODE
    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
        return MESSAGE_CODE
    return None
