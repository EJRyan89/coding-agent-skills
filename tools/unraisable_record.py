"""Record each unraisable exception a Python process meets, for the scheduled Python diagnostic run.

tools/python_diagnostics.py copies this module, as sitecustomize.py, into a folder of its own that it puts first on
PYTHONPATH, so every Python process its suites start with that environment imports it at startup. Imported under that
name, it installs a sys.unraisablehook that appends one line for each unraisable exception, such as the ResourceWarning
development mode and PYTHONWARNINGS=error raise in the destructor of a file left open, to a file of the process's own in
the folder RECORD_VARIABLE names, and then hands the exception to the hook it replaced, which prints it as before. Such
an exception never changes the process's exit status, so the record is how the run sees one in a process whose caller
reads only its status. The line is built from what cannot raise: an object whose repr raises is shown by its type,
and an exception whose text raises as Python's own hook shows it. A process that cannot write its line, as when the
folder is gone, it is out of file descriptors, or it is kept from the folder, still hands the exception on, then says
why on its error output and ends with UNRECORDED_STATUS, so a caller that reads its status sees it fail rather than
read as a process that met none. A caller that reads neither its status nor its error output does not see it. It then
runs the sitecustomize the interpreter would have found without it, if there is one, as a Homebrew Python has. It
imports nothing outside the standard library, since every process imports it before its own path is set.
"""

from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import os
import reprlib
import sys
import traceback
from collections.abc import Callable, Mapping
from pathlib import Path

# The folder each process appends its unraisable exceptions to, one file a process, named by its process id.
RECORD_VARIABLE = "PYTHON_DIAGNOSTICS_RECORD"
SITE_MODULE = "sitecustomize"
# The status a process ends with when it met an unraisable exception and could not record it, apart from the 0 of a
# process that met none and the 1 of an uncaught exception: EX_SOFTWARE in sysexits.h.
UNRECORDED_STATUS = 70
# Why a line went unwritten when what stopped it was not an OSError, which names its own cause.
UNFINISHED = "building or writing its line stopped before it finished"
# Shows an object without raising: reprlib shows one whose repr raises by its type and address. Its bounds are wide
# enough for the file, socket, or process a destructor reports, and mark a longer repr cut with "...".
SHOWN = reprlib.Repr(
    maxlevel=6,
    maxtuple=100,
    maxlist=100,
    maxarray=100,
    maxdict=100,
    maxset=100,
    maxfrozenset=100,
    maxdeque=100,
    maxstring=2000,
    maxlong=2000,
    maxother=2000,
)
INDIRECTION_ALLOWED = {
    "run_shadowed": "runs the sitecustomize module this copy hides, which only the import system's path finder can "
    "find, with the standard library alone, since every process imports this before its own path is set",
}


def record_line(unraisable: sys.UnraisableHookArgs, arguments: object) -> str:
    """One line naming the process by its arguments, the exception, and what Python says it was finalizing, its
    whitespace flattened, built so that no repr or text it shows can make it raise: traceback shows an exception whose
    text raises as `<exception str() failed>`, as Python's own hook does."""
    shown = (
        traceback.format_exception_only(unraisable.exc_value)
        if unraisable.exc_value is not None
        else [unraisable.exc_type.__name__]
    )
    message = unraisable.err_msg or "Exception ignored in"
    seen = f"{message}: {SHOWN.repr(unraisable.object)}: {''.join(shown)}"
    return " ".join(f"{SHOWN.repr(arguments)} {seen}".split()) + "\n"


def end_unrecorded(folder: Path, reason: str) -> None:
    """Say on the error output, which may itself be gone at shutdown, that an unraisable exception went unrecorded and
    why, and end the process with UNRECORDED_STATUS."""
    said = f"unraisable_record: could not record an unraisable exception in {folder}: {reason}"
    with contextlib.suppress(OSError):
        os.write(2, (" ".join(said.split()) + "\n").encode("utf-8", "backslashreplace"))
    os._exit(UNRECORDED_STATUS)


def recording_hook(
    folder: Path, previous: Callable[[sys.UnraisableHookArgs], object], arguments: object
) -> Callable[[sys.UnraisableHookArgs], None]:
    """A hook that appends each unraisable exception's line to this process's file in folder, naming the process by
    arguments, then calls previous, whether or not the line could be written, and ends the process when it was not,
    whatever stopped it."""

    def hook(unraisable: sys.UnraisableHookArgs) -> None:
        unwritten: str | None = UNFINISHED
        try:
            line = record_line(unraisable, arguments)
            with (folder / f"{os.getpid()}.txt").open("a", encoding="utf-8", errors="backslashreplace") as record:
                record.write(line)
            unwritten = None
        except OSError as error:
            unwritten = str(error)
        finally:
            try:
                previous(unraisable)
            finally:
                if unwritten is not None:
                    end_unrecorded(folder, unwritten)

    return hook


def install(environment: Mapping[str, str]) -> bool:
    """Install the recording hook when environment names a record folder, and say whether it did."""
    folder = environment.get(RECORD_VARIABLE)
    if not folder:
        return False
    # The arguments list itself, not a copy: runpy sets its first item in place for `-m`, and the interpreter sets
    # sys.argv to None as it shuts down, when a destructor's unraisable exception is most often met.
    sys.unraisablehook = recording_hook(Path(folder), sys.unraisablehook, sys.argv)
    return True


def run_shadowed(own_folder: Path, search_path: list[str]) -> bool:
    """Run the sitecustomize module this copy hides from the interpreter, the first one on search_path outside
    own_folder, an absolute path as the interpreter found this copy in, and say whether there was one."""
    others = [entry for entry in search_path if Path(entry or os.curdir).absolute() != own_folder]
    spec = importlib.machinery.PathFinder.find_spec(SITE_MODULE, others)
    if spec is None or spec.loader is None:
        return False
    spec.loader.exec_module(importlib.util.module_from_spec(spec))
    return True


if __name__ == SITE_MODULE:
    install(os.environ)
    run_shadowed(Path(__file__).absolute().parent, sys.path)
