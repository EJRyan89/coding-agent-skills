"""Console setup shared by every skill entry point.

Skill output names paths, titles, and text the user wrote, and a Windows pipe defaults to a legacy code page that
cannot encode all of it, so printing would raise UnicodeEncodeError. An entry point calls `use_utf8_output()` first
in its `if __name__ == "__main__":` block.
"""

from __future__ import annotations

import io
import sys


def use_utf8_output(*, errors: str = "strict", newline: str | None = None) -> None:
    """Write UTF-8 to stdout and stderr.

    `errors` applies to stdout, such as "replace" for a script that prints paths Git gave it with surrogate escapes;
    stderr keeps Python's own "backslashreplace", which an encoding-only reconfigure would turn into "strict".
    `newline`, when given, sets both streams' line ending; otherwise they keep the platform's. A stream that is not a
    text wrapper, such as a test's StringIO, is left alone.
    """
    for stream, stream_errors in ((sys.stdout, errors), (sys.stderr, "backslashreplace")):
        if not isinstance(stream, io.TextIOWrapper):
            continue
        if newline is None:
            stream.reconfigure(encoding="utf-8", errors=stream_errors)
        else:
            stream.reconfigure(encoding="utf-8", errors=stream_errors, newline=newline)
