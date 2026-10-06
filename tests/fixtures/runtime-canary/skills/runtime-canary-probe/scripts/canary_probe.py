"""Record that a runtime ran this script: where the script was, the directory it ran from, and its arguments.

The record is appended to .runtime-canary/probe.jsonl in the home this skill was deployed into. That home is found
from this file's own location, <home>/.claude/skills/runtime-canary-probe/scripts/, and never from the environment,
so a record exists only when the runtime resolved the skill's directory to the deployed copy.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main(arguments: list[str]) -> int:
    script = Path(__file__).resolve()
    log = script.parents[4] / ".runtime-canary" / "probe.jsonl"
    log.parent.mkdir(exist_ok=True)
    record = {"script": script.as_posix(), "cwd": Path.cwd().as_posix(), "arguments": arguments}
    with log.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record) + "\n")
    print(f"RUNTIME_CANARY_PROBE RAN {script.as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
