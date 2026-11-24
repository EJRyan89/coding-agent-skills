"""Record each unraisable exception a Python process meets, for the scheduled Python diagnostic run.

tools/python_diagnostics.py copies this module, as sitecustomize.py, into a folder of its own that it puts first on
PYTHONPATH, so every Python process its suites start with that environment imports it at startup. Imported under that
name, it installs a sys.unraisablehook that appends one line for each unraisable exception, such as the ResourceWarning
development mode and PYTHONWARNINGS=error raise in the destructor of a file left open, to a file of the process's own in
the folder RECORD_VARIABLE names, and then hands the exception to the hook it replaced, which prints it as before. Such
an exception never changes the process's exit status, so the record is how the run sees one in a process whose caller
reads only its status. It then runs the sitecustomize the interpreter would have found without it, if there is one, as a
Homebrew Python has. It imports nothing outside the standard library, since every process imports it before its own
path is set.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

# The folder each process appends its unraisable exceptions to, one file a process, named by its process id.
RECORD_VARIABLE = "PYTHON_DIAGNOSTICS_RECORD"
SITE_MODULE = "sitecustomize"
INDIRECTION_ALLOWED = {
    "run_shadowed": "runs the sitecustomize module this copy hides, which only the import system's path finder can "
    "find, with the standard library alone, since every process imports this before its own path is set",
}


def record_line(unraisable: sys.UnraisableHookArgs, arguments: object) -> str:
    """One line naming the process by its arguments, the exception, and what Python says it was finalizing, its
    whitespace flattened."""
    exception = unraisable.exc_type.__name__
    message = unraisable.err_msg or "Exception ignored in"
    seen = f"{message}: {unraisable.object!r}: {exception}: {unraisable.exc_value}"
    return " ".join(f"{arguments!r} {seen}".split()) + "\n"


def recording_hook(
    folder: Path, previous: Callable[[sys.UnraisableHookArgs], object], arguments: object
) -> Callable[[sys.UnraisableHookArgs], None]:
    """A hook that appends each unraisable exception's line to this process's file in folder, naming the process by
    arguments, then calls previous, whether or not the line could be written."""

    def hook(unraisable: sys.UnraisableHookArgs) -> None:
        try:
            with (folder / f"{os.getpid()}.txt").open("a", encoding="utf-8") as record:
                record.write(record_line(unraisable, arguments))
        finally:
            previous(unraisable)

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
