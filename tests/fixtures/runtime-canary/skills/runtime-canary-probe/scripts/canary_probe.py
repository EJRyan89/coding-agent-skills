"""Record that a runtime ran this script: where the script was and the directory it ran from.

Both canaries deploy it four folders below the folder that holds their .runtime-canary state: tools/runtime_canary.py
at <home>/.claude/skills/runtime-canary-probe/scripts/, and tools/plugin_canary.py at
<install path>/skills/runtime-canary-probe/scripts/, whose state sits beside the install path. The probe finds that
state from this file's own location, never from the environment, and appends its record to probe.jsonl there, so a
record names the copy the runtime resolved the skill's directory to. The record carries the run's identifier, which
the canary writes to run-id in the same state folder before each run; with no identifier to read, the probe writes
nothing, says why on stderr, and exits 1. A canary counts a record that carries the run's identifier and names this
script under the copy it deployed, so a line that anything appends without the identifier does not count; the
identifier is in the home for anything there to read, so it shows which run a record belongs to, not that this
script wrote it, which the canary's own script recorders show.

It is no entry point validation reads, since nothing under tests/fixtures ships, and it calls no ejryan89-skill-core
runner: its source cannot declare that skill, and the plugin it is built into carries no copy of it. Its suite is
tests/tools/test_canary_probe.py, which runs it in both layouts.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    script = Path(__file__).resolve()
    state = script.parents[4] / ".runtime-canary"
    identifier = state / "run-id"
    try:
        run = identifier.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        print(f"FAILED cannot read the run identifier in {identifier.as_posix()}: {exc}", file=sys.stderr)
        return 1
    if not run:
        print(f"FAILED the run identifier in {identifier.as_posix()} is empty", file=sys.stderr)
        return 1
    record = {"script": script.as_posix(), "cwd": Path.cwd().as_posix(), "run": run}
    with (state / "probe.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record) + "\n")
    print(f"RUNTIME_CANARY_PROBE RAN {script.as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
