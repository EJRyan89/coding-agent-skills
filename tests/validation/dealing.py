"""The one dealer of a run's work across shards, and the one reader of a shard's `<index>/<count>`.

The root conftest.py deals a suite's tests across the shards the runner splits it into, and job_selection.py deals the
selected policy checks and suite jobs across CI's legs, the same way: the heaviest first onto the lightest. The
runner's --shard and conftest.py's --suite-shard name one shard alike. Neither needs pytest, so this module imports
none, and the runner and the policies import it without the root conftest.py.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

SHARD = re.compile(r"([1-9]\d*)/([1-9]\d*)")


def deal(names: list[str], count: int, seconds: Mapping[str, float]) -> dict[str, int]:
    """Each test's shard: heaviest first onto the shard with the least so far, the lowest index on a tie, where a test
    weighs its recorded seconds or else one. With nothing recorded that is round-robin in test-name order."""
    loads = [0.0] * count
    shards: dict[str, int] = {}
    for name in sorted(names, key=lambda name: (-seconds.get(name, 1), name)):
        shard = min(range(count), key=lambda candidate: (loads[candidate], candidate))
        shards[name] = shard
        loads[shard] += seconds.get(name, 1)
    return shards


INT_PARSE_ALLOWED = {
    "shard_of": "it turns the ValueError int() raises past the digit limit into its own refusal",
}


def shard_of(text: str) -> tuple[int, int]:
    """A shard's `<index>/<count>`, the index counted from 1. Raises ValueError naming what it expects, which each
    caller turns into its own usage error."""
    expected = ValueError(f"<index>/<count> with 1 <= index <= count, found {text!r}")
    match = SHARD.fullmatch(text)
    if match is None:
        raise expected
    try:
        index, count = int(match.group(1)), int(match.group(2))
    except ValueError:
        # A number past the interpreter's digit limit for converting text.
        raise expected from None
    if index > count:
        raise expected
    return index, count
