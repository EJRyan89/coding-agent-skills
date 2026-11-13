"""Claude Code's `--output-format stream-json` output, read as the events it holds: the one reader of that stream for
tools/runtime_canary.py, tools/plugin_canary.py, and tools/skill_evals_runs.py.

Claude Code writes one JSON object to a line, so the stream is split on a newline alone, as
`deployer.discovery.output_lines` splits it: never on the other characters str.splitlines() breaks on, such as U+0085,
U+2028, and U+2029, which a JSON string Claude Code writes as it is can hold, and which would cut one event into two
lines, neither of them JSON.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "ejryan89-skill-core" / "scripts"))

import json_reader

from deployer import discovery

# How a JSON value of each kind but an object is named in a refusal.
KINDS = {list: "array", str: "string", int: "number", float: "number", bool: "boolean", type(None): "null"}


def json_kind(value: object) -> str:
    """How a JSON value that is no object is named in a refusal, by this reader and tools/runtime_canary.py's
    read_records alike."""
    return KINDS.get(type(value), type(value).__name__)


class StreamError(ValueError):
    """A line of the stream that is JSON but no event object: a stream in a shape this does not read."""


def events(output: str) -> Iterator[dict[str, Any]]:
    """Each event object the stream holds, in order. A line that is JSON of another kind than an object raises
    StreamError naming its line, since every event Claude Code writes is an object, and passing such a line over would
    judge the stream from the events left. A line that is not JSON, or is JSON json_reader refuses, is passed over: it
    is no event of the session's, as a blank line, a warning, or the last line cut off part way when the session was
    ended at its time limit is not, and the events around it are still the session's."""
    for number, line in enumerate(discovery.output_lines(output), 1):
        try:
            event = json_reader.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            raise StreamError(
                f"line {number} of the session's stream is a JSON {json_kind(event)}, not an event object"
            )
        yield event
