"""Console setup and the error contract shared by every skill entry point.

Skill output names paths, titles, and text the user wrote, and a Windows pipe defaults to a legacy code page that
cannot encode all of it, so printing would raise UnicodeEncodeError. An entry point's `if __name__ == "__main__":`
block is one call, `run_main(main)`, which sets UTF-8 output first and turns a failure `main` did not handle into a
`FAILED` line instead of a traceback.

deploy.py imports this module before its Python version check, so it keeps to a grammar Python 3.7 reads.
"""

from __future__ import annotations

import io
import sys
import traceback
from collections.abc import Callable
from typing import NoReturn

from flat_text import flat_text

# Ctrl+C exits as a shell reports a command SIGINT stopped, as deploy.py does.
INTERRUPTED_EXIT = 130
INTERRUPTED_LINE = "FAILED interrupted"


def use_utf8_output(*, errors: str = "strict", newline: str | None = None) -> None:
    """Write UTF-8 to stdout and stderr.

    `errors` applies to stdout, such as "replace" for a script that prints paths Git gave it with surrogate escapes;
    stderr keeps Python's own "backslashreplace", which an encoding-only reconfigure would turn into "strict".
    `newline`, when given, sets both streams' line ending; otherwise they keep the platform's. A stream that is not a
    text wrapper, such as a test's StringIO, is left alone.
    """
    _use_utf8(sys.stdout, errors, newline)
    _use_utf8(sys.stderr, "backslashreplace", newline)


def _use_utf8(stream: object, errors: str, newline: str | None) -> None:
    """Write UTF-8 to one stream with these errors and, when given, this line ending, unless it is no text wrapper."""
    if not isinstance(stream, io.TextIOWrapper):
        return
    if newline is None:
        stream.reconfigure(encoding="utf-8", errors=errors)
    else:
        stream.reconfigure(encoding="utf-8", errors=errors, newline=newline)


def failed_line(error: BaseException) -> str:
    """The line an unhandled exception ends a script with: `FAILED <type>: <message>`, the message on one line.

    A character UTF-8 cannot encode, such as a surrogate-escaped byte of a path, is written as its escape, so the line
    prints on a strict stream.
    """
    message = flat_text(str(error)).encode("utf-8", "backslashreplace").decode("utf-8")
    return f"FAILED {type(error).__name__}: {message}" if message else f"FAILED {type(error).__name__}"


def run_main(
    main: Callable[[], int | None],
    *,
    errors: str = "strict",
    newline: str | None = None,
    traceback_requested: Callable[[], bool] | None = None,
) -> NoReturn:
    """Set up the console as `use_utf8_output` does, run `main`, and exit with what it returns.

    A `SystemExit`, argparse's usage error included, passes through. Any other exception prints `failed_line` on
    stdout and exits 1, and Ctrl+C prints `FAILED interrupted` and exits 130; neither prints a traceback unless
    `traceback_requested`, asked only then, says to, as deploy.py's --debug does. A failure the script expects stays
    its own to report, with its own line, before it reaches here.
    """
    use_utf8_output(errors=errors, newline=newline)
    try:
        code = main()
    except KeyboardInterrupt as error:
        print(INTERRUPTED_LINE, flush=True)
        _print_requested_traceback(error, traceback_requested)
        raise SystemExit(INTERRUPTED_EXIT) from None
    except Exception as error:  # noqa: BLE001 - the last handler: any exception ends as one FAILED line
        print(failed_line(error), flush=True)
        _print_requested_traceback(error, traceback_requested)
        raise SystemExit(1) from None
    raise SystemExit(code)


def _print_requested_traceback(error: BaseException, requested: Callable[[], bool] | None) -> None:
    """Write `error`'s traceback, its causes included, to stderr after its line when `requested` says to.

    A request that fails to answer shows none, so the line stays the whole report, as it is without one.
    """
    if requested is None:
        return
    try:
        wanted = requested()
    except Exception:  # noqa: BLE001 - a request that fails to answer shows no traceback
        return
    if wanted:
        # The three-argument form, which Python 3.7 reads; see the module docstring.
        sys.stderr.write("".join(traceback.format_exception(type(error), error, error.__traceback__)))
        sys.stderr.flush()
