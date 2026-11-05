"""The wall-clock limit the hub guard's tests hand each process a reading starts, git or pwsh.

A reading in a test's own process looks at the time through `hub_guard.reading_clock`, which a test holds still, but
the processes it starts each get a real limit through `hub_guard.process_seconds`: what is left of the reading time,
or of a git call's own. A loaded runner can take that long to start pwsh, so a test that left the limit real would
race a real start against it and fail at random on the time-out rather than on the rule it names. Each suite that
reads a command in its own process holds the limit here instead, and asserts on the limit the guard asked for. The
suites under `tests/tools/` import it from beside them; it holds no test.
"""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools import hub_guard

# Far past any start a loaded runner takes, and still a bound, so a process that hangs ends the test rather than the
# run. No reading time is this long, so a limit the guard hands out without asking process_seconds never equals it.
PROCESS_SECONDS = 600.0


@contextlib.contextmanager
def process_limits_held() -> Iterator[list[float]]:
    """Hands every process the guard starts PROCESS_SECONDS, and yields the limits the guard asked for, in order."""
    asked: list[float] = []

    def limit(seconds: float) -> float:
        asked.append(seconds)
        return PROCESS_SECONDS

    with mock.patch.object(hub_guard, "process_seconds", side_effect=limit):
        yield asked
