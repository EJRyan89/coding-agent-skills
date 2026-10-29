"""The repository's one table each of the calls that start a process and the calls that open text.

Every policy that reads one of these families reads it here: the commands skills may run and the shells they run
through (skill_scripts.py), the environment a process starts with and the encoding its output is decoded with
(python_checks.py), and the opens that may write (fsops_platform.py). A call the standard library adds to a family is
added here once, and reaches each of them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# An event loop's runner methods count on any receiver, since a reader cannot tell which object is an event loop.
EVENT_LOOP = "<event loop>"
# The position of the last positional argument, for a call whose arguments run on until the one named.
LAST = -1


@dataclass(frozen=True)
class ProcessStart:
    """A call that starts a process: the position and keyword of the argument that names its command, a list or tuple
    whose first item is the program or a string whose first word is; where it takes the environment the process
    starts with, by position (None for a keyword alone, LAST for the last positional argument) and keyword, or None for
    a call that takes none, whose process inherits this one's; how its output reaches the caller, decoded as text
    "always", when a text= or universal_newlines= option asks ("option"), or "never"; whether it runs its command
    through a shell; whether it hands its keywords to subprocess.Popen, whose executable= replaces the program the
    command names; and whether it clears git's repository-selecting variables from the environment itself."""

    command: tuple[int, str]
    environment: tuple[int | None, str] | None
    text: Literal["always", "option", "never"] = "never"
    through_shell: bool = False
    popen_keywords: bool = False
    clears: bool = False


_SUBPROCESS = ProcessStart((0, "args"), (None, "env"), "option", popen_keywords=True)
_INHERITS_OS = ProcessStart((0, "file"), None)
_SPAWN_INHERITS = ProcessStart((1, "file"), None)

# Every call that starts a process, by the name a module's imports resolve it to. The exec and spawn calls of os take
# the program itself, by path or by name, ahead of its arguments, and a spawn takes its mode first.
PROCESS_STARTS: dict[str, ProcessStart] = {
    **{f"subprocess.{name}": _SUBPROCESS for name in ("run", "Popen", "call", "check_call", "check_output")},
    "subprocess.getoutput": ProcessStart((0, "cmd"), None, "always", through_shell=True),
    "subprocess.getstatusoutput": ProcessStart((0, "cmd"), None, "always", through_shell=True),
    "os.system": ProcessStart((0, "command"), None, through_shell=True),
    "os.popen": ProcessStart((0, "cmd"), None, "always", through_shell=True),
    "os.execv": ProcessStart((0, "path"), None),
    "os.execve": ProcessStart((0, "path"), (2, "env")),
    **{f"os.{name}": _INHERITS_OS for name in ("execl", "execlp", "execvp")},
    **{f"os.{name}": ProcessStart((0, "file"), (LAST, "env")) for name in ("execle", "execlpe")},
    "os.execvpe": ProcessStart((0, "file"), (2, "env")),
    **{f"os.{name}": _SPAWN_INHERITS for name in ("spawnl", "spawnlp", "spawnv", "spawnvp")},
    **{f"os.{name}": ProcessStart((1, "file"), (LAST, "env")) for name in ("spawnle", "spawnlpe")},
    **{f"os.{name}": ProcessStart((1, "file"), (3, "env")) for name in ("spawnve", "spawnvpe")},
    "os.posix_spawn": ProcessStart((0, "path"), (2, "env")),
    "os.posix_spawnp": ProcessStart((0, "path"), (2, "env")),
    "os.startfile": ProcessStart((0, "path"), None),
    "pty.spawn": ProcessStart((0, "argv"), None),
    **{
        f"{module}.create_subprocess_exec": ProcessStart((0, "program"), (None, "env"), popen_keywords=True)
        for module in ("asyncio", "asyncio.subprocess")
    },
    **{
        f"{module}.create_subprocess_shell": ProcessStart(
            (0, "cmd"), (None, "env"), through_shell=True, popen_keywords=True
        )
        for module in ("asyncio", "asyncio.subprocess")
    },
    f"{EVENT_LOOP}.subprocess_exec": ProcessStart((1, "program"), (None, "env"), popen_keywords=True),
    f"{EVENT_LOOP}.subprocess_shell": ProcessStart((1, "cmd"), (None, "env"), through_shell=True, popen_keywords=True),
    # ejryan89-skill-core's runner, which bounds a command with a time limit, gives it no stdin, and starts it without
    # git's repository-selecting variables whatever environment it is given.
    "bounded_process.run_bounded": ProcessStart((0, "command"), (None, "env"), clears=True),
    "bounded_process.streaming": ProcessStart((0, "command"), (None, "env"), clears=True),
}
# The name each event loop's runner has, which a call on any receiver may name.
EVENT_LOOP_STARTS = frozenset(name.rpartition(".")[2] for name in PROCESS_STARTS if name.startswith(f"{EVENT_LOOP}."))


@dataclass(frozen=True)
class TextOpener:
    """Where a call that may open text takes its mode and its encoding, by position (None for a keyword alone or no
    mode at all), the mode it opens in when given none, empty for a call that opens only text, and whether it opens a
    plain file by name with the built-in open's arguments, its mode second."""

    mode: int | None
    default_mode: str
    encoding: int | None
    plain_file: bool = False


BINARY_DEFAULT = "rb"
# The module functions and classes that open text, by the name they are imported as. codecs.open given no encoding
# opens the file with the built-in open, in text.
TEXT_OPENERS = {
    "open": TextOpener(1, "r", 3, plain_file=True),
    "builtins.open": TextOpener(1, "r", 3, plain_file=True),
    "io.open": TextOpener(1, "r", 3, plain_file=True),
    "codecs.open": TextOpener(1, "r", 2, plain_file=True),
    "os.fdopen": TextOpener(1, "r", 3),
    "io.TextIOWrapper": TextOpener(None, "", 1),
    "tempfile.TemporaryFile": TextOpener(0, "w+b", 2),
    "tempfile.NamedTemporaryFile": TextOpener(0, "w+b", 2),
    "tempfile.SpooledTemporaryFile": TextOpener(1, "w+b", 3),
    "gzip.open": TextOpener(1, BINARY_DEFAULT, 3),
    "bz2.open": TextOpener(1, BINARY_DEFAULT, 3),
    "lzma.open": TextOpener(1, BINARY_DEFAULT, None),
}
# The path methods that open, read, or write text, by the attribute a call names: pathlib's and zipfile's alike.
TEXT_METHODS = {
    "read_text": TextOpener(None, "", 0),
    "write_text": TextOpener(None, "", 1),
    "open": TextOpener(0, "r", 2),
}
# The module functions named open that take no text encoding: a call through one is no path's open.
OTHER_OPENS = frozenset({"os.open", "tarfile.open", "shelve.open", "dbm.open", "webbrowser.open", "wave.open"})
