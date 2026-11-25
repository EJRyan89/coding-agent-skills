"""Print the arguments a runtime ran this script with, for the runtime canary's expansion probe.

The skill's one grant covers this script with any arguments, and its fence runs it once with the word `literal` and
once with `"$PWD"`, so the two commands differ only in that the second expands a shell variable. The canary judges
the runs from its own Python recorder, which keeps each run's arguments, and from Claude Code's permission denials,
never from what this prints.

It is no entry point validation reads, since nothing under tests/fixtures ships. Its suite is
tests/tools/test_runtime_canary.py.
"""

from __future__ import annotations

import sys


def main() -> int:
    print("RUNTIME_CANARY_EXPANSION " + " ".join(sys.argv[1:]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
